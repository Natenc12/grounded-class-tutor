> **Direction changed October 2, 2026:** Nathan selected a local desktop app using his ChatGPT plan. The hosted deployment proposal below is retained for reference. Its cloud release gates are no longer the next milestone. The local proof and remaining decisions are documented in [the local app proof](local-app-proof.md).

# Grounded Class Tutor Agent Launch Plan

This plan finishes Grounded Class Tutor as a private hosted application Nathan can use on a laptop or phone and demonstrate. It defines implementation packages, agent responsibilities, verification, and release conditions. The implementation keeps the existing grounding core and Postgres worker queue.

**Plan date:** October 2, 2026. **Inspected baseline:** `156cf13` on clean `main`. **Cost direction:** Nathan prefers a free hosting option and has not used Supabase. No paid hosting or runtime API allowance is approved. This is a design and execution specification, not evidence that the application has been implemented or deployed. The GitHub issue board continues to own actual assignments, dependency status, and completion.

## Release outcome

Nathan can sign in, reopen saved classes, upload supported PDF/PPTX course material, see each file become ready or fail with a useful explanation, and ask a question. Answers display source filenames and page/slide citations. Unsupported questions receive an honest refusal; partial and integrity-flagged responses have distinct treatments. Refreshing, returning on another device, and restarting the API or worker preserve accepted work.

The free pilot can sleep while unused. Processing pauses with the host and resumes after wake and lease recovery, or reports an actionable terminal failure if its bounded retry policy is exhausted. The UI must explain waiting and recovery; no uninterrupted background-processing promise is made. Supabase may separately pause an inactive free project and require an owner to resume it in its dashboard. Confirmed durability does not imply constant availability.

This release serves one explicitly allowed owner. It adds limited sign-in ahead of the existing V3 accounts milestone because the selected release must be private. General registration, multiple users, advanced retrieval tuning, OCR, quizzes, offline PWA behavior, deletion, and source-page preview remain separate work. Mobile responsiveness is included. Formal V3 numerical quality claims remain subject to the existing evaluation requirements.

## Decisions for implementation

These are proposed implementation defaults. Package A records the small necessary ADR amendments before dependent code lands; this document does not silently override existing ADRs.

| Decision | Selected design | Reason and boundary |
| --- | --- | --- |
| Hosting | Candidate: one Render Free Docker web service serves React and supervises separate API and worker OS processes. | Keeps one origin and the existing process boundary. Shared 0.1 CPU/512 MB capacity, supervision, and interruption behavior must pass a feasibility gate. A dedicated Render background-worker service is not free. |
| Database and queue | Supabase Free Postgres with pgvector; retain the current database job queue. | Keep existing transactions, lease/retry behavior, and retrieval. Measure database growth and polling traffic against free quotas. |
| Upload storage | Private Supabase Free Storage; an adapter materializes immutable objects to worker-local temporary files. | Container disk is disposable. Preserve the original filename separately from the opaque storage reference, even if both processes initially share one container. |
| Sign-in | Supabase Auth with registration and anonymous sign-in disabled; allow exactly one configured subject ID in FastAPI. | Reuse the planned Supabase service and remove a separate access-proxy dependency. A generic sign-in shell may be public; every course-data and paid-work endpoint is protected at the origin. |
| Data exposure | Browser uses Supabase only for Auth. Application data goes through FastAPI. Disable the Supabase Data API for the backend-only database and deny browser object access. | Avoid a second path around application authorization. Service credentials never enter the browser. |
| Spending | A persistent, atomic reservation ledger governs every actual paid provider attempt from both API and worker. | Billing alerts are supplemental. Explicit input, output, retry, and concurrency bounds make admission enforceable. |
| Delivery | One versioned image includes process supervision, API and worker commands, and the built client. An authorized operator/local runner performs migrations. | Free Render does not supply the paid pre-deploy hook or one-off jobs. Record the exact source/image/build and retain a known previous candidate for rollback. |

Render supports Docker web services; Docker supports supervised multiple processes. Co-locating this API and worker is an engineering proposal, not a provider-certified GCT deployment. Supabase supports disabling new signups, verifying user tokens, and disabling its Data API when data is accessed through trusted direct database connections. These capabilities support a prototype, not a claim that the accounts or deployment already exist. See the primary sources below.

Supabase Free currently includes a 500 MB database, 1 GB object storage, a 50 MB upload limit, and 5 GB uncached egress; automatic backups are not included. Inactive free projects can pause after a week. Render Free can sleep after 15 idle minutes, take roughly a minute to wake, lose all local files, exhaust monthly quotas, or suspend unusually high service-initiated traffic. Keep infrastructure on free plans and avoid payment-backed overages; verify the actual account settings. No artificial keepalive is part of this design. Select corpus/upload limits from measurements and keep them below the exact provider limits.

Vercel is a service for deploying websites and request-driven backend functions; its Hobby plan is free for personal, noncommercial projects. It can host the React client and some Python HTTP code. Vercel functions do not host the existing indefinite worker loop unchanged, and their payload/time limits require a different ingestion/upload design. Vercel plus Supabase alone is therefore not the smallest change for this codebase. Do not add a third hosting service solely to host the static client.

Free hosting does not pay for OpenAI embeddings or generation. Development can use fake providers for zero provider spend. Live inference stays disabled until a separate allowance is approved and configured; a fixture-only demo must be identified as such. If the free compute prototype fails, report the measured failure and choose a smaller supported corpus, an explicit serverless redesign, a disclosed Mac-dependent deployment, or separately approved paid compute. Never silently upgrade a service. On the chosen free infrastructure configuration, quota exhaustion must suspend or reject work instead of incurring payment-backed overages; verify the actual account behavior before launch.

A shared-volume VM was considered because it would preserve existing path-based staging. It is not the default: no usable VM or operations owner has been verified, and introducing server maintenance, TLS, backups, and supervision would trade application work for operational work.

## Agent roster and actual capacity

Fifteen roles form a specialist roster: two coordination roles, seven builder specialties, three reviewers, and three governors. They are dispatched as useful jobs. The current runtime supports **four concurrent agents in total**, including the lead. No task may assume fifteen simultaneous workers or use another launcher to bypass this limit.

The lead normally occupies one slot and combines dispatch and integration. Three remaining slots execute the highest-priority ready work. Short specialist reviews and governance jobs borrow one of those slots, then finish. Additional supported capacity can be adopted only after it is actually available; the dependency and ownership rules stay the same.

| Role | Responsibility |
| --- | --- |
| Lead and dispatcher | Own scope, decisions, priorities, dependency reconciliation, and the final go/no-go decision. |
| Integrator and release owner | Combine small commits, preserve shared interfaces, run combined checks, and assemble the exact release candidate. The lead wears this role at current capacity. |
| App shell builder | Class selection, questions, session/reload behavior, global styling, and component composition. |
| Upload builder | Multi-file upload, per-file status, polling, and failure remedies. |
| Answer builder | Pure rendering of answers, gaps, refusals, integrity warnings, and resolved citations. |
| Library API builder | Owner-scoped class/file listing and canonical API schemas/generated client types. |
| Storage builder | Durable upload references, worker materialization, retries, and filename preservation. |
| Deployment builder | Container/image, hosted services, configuration, migration and rollback commands. |
| Access and budget builder | Verified owner identity, origin protection, rate limits, provider-call admission, and spend accounting. |
| Browser reviewer | Required journeys, state/race behavior, responsive layout, and accessibility. |
| Grounding reviewer | Compare claims and citations against actual source passages on the final configuration. |
| Security and recovery reviewer | Access boundaries, private data, spending concurrency, restart and persistence behavior. |
| Delivery governor | Inspect blocked work, ownership collisions, duplicate work, and integration backlog. |
| Evidence governor | Check candidate identity, missing/skipped checks, stale signoffs, and release claims. |
| Improvement governor | Propose a small repair for a demonstrated recurring process/tool failure and measure the result. |

Use Astra for lead decisions and demanding independent reviews; use Sol for well-defined implementation packages. The model choice is secondary to clear inputs and reproducible acceptance evidence. Model agreement is not a test result.

## Work packages and dependencies

These packages define the proposed launch work. They are not a second live task board. Reuse existing issues when their scope matches; put any required splits, new dependencies, and assignments on GitHub when implementation is authorized. Relevant existing work includes #140 shell completion, #141 corpus UI, #142 answer UI, #143 the UI demonstration, #157 frontend CI, and #149 the authoritative upload-byte bound. Verify issue state at pickup.

| Package | Deliverable | Depends on | Completion evidence |
| --- | --- | --- | --- |
| A — Bootstrap and contracts | Amend the private-release decisions; freeze component props, list schemas, storage reference, and auth/budget/recovery states; add client test tools and CI; specify the free-runtime prototype. | Current repository and issue inspection | A component test and browser-test runner pass, fixtures import, frontend CI runs, scratch database setup works, and ownership handoffs are recorded. |
| B — Library API | Owner-scoped class and file lists, generated types, and typed API wrappers. | A | Empty/list/foreign-owner cases pass; generated types match the server snapshot. |
| C — Upload interface | Multi-file acceptance, actual status polling, terminal reasons, readiness, and cleanup. | A | Deterministic tests cover errors, unknown status, late replies, terminal stop, and class change. |
| D — Answer interface | Citation chips, paragraphs, partial gaps, refusal, flagged output, and transport-error treatment. | A | All states and malformed/unresolved labels render safely and distinctly. |
| E — Application integration | Sign-in shell, server-loaded classes/files, question flow, stale-response protection, desktop/phone layout. | A; B/C/D contracts permit fixture work before their implementations land | Primary browser journey and reload/class-switch scenarios pass. Real sign-in completes with G. |
| F — Durable storage | Private immutable objects, original filenames, materialization within lease protection, and recovery at write boundaries. | A | Separate API/worker filesystems, restart, orphan, and publication tests pass without lost accepted files or partial chunks. |
| G — Access and spending | One verified owner, protected data/cost routes, disabled alternate data access, bounded uploads, atomic paid-attempt admission, and clear budget-stop behavior. | A; F storage contract | Invalid/foreign/expired tokens fail; concurrent API/worker and timeout/retry tests respect the budget; rejected work makes zero provider calls. |
| H — Deployment candidate | Versioned supervised image, same-origin routing, free-plan migration runner, secrets, redacted logs, backup/restore and rollback procedure; measured capacity and sleep/recovery prototype. | A; F/G contracts allow scaffolding in parallel | Local constrained-capacity, child-process failure, whole-runtime interruption, and restore/rollback rehearsals pass. Hosted platform behavior remains I's gate. |
| I — Hosted acceptance | Deploy the gated candidate and perform functional, grounding, access, persistence, spending, and phone checks. | B–H; approved accounts, identities, and cost ceilings | All blocking gates pass on the same deployed candidate; private evidence and a shareable redacted demo are separated. |

The access/budget work can be split into independent owned modules once A defines their contracts. The deployment builder owns the complete exposure configuration; the security reviewer checks it end to end. Interface publication unblocks fixture work, but dependent implementation is not accepted until the real integration passes.

## Dispatch sequence

1. **Bootstrap:** lead settles the contracts; specialists complete frontend CI/test setup, contract inspection, and storage/provider-boundary inspection. Finish A with one coherent baseline commit.
2. **First parallel build:** three workers execute B, C, and D. The lead reviews and integrates small commits without becoming the principal shell implementer. The first suitable released builder owns E and its shared frontend files. Do not defer every merge until the features are finished.
3. **Hosted capability build:** prioritize H's constrained-runtime prototype early, then dispatch F, G, and remaining H work as interfaces and slots permit. E competes in this same three-slot queue; it is not hidden extra capacity. The free prototype must succeed before committing to extensive host-specific work.
4. **Independent acceptance preparation:** finish or pause a builder at a clean checkpoint to reserve a slot for a reviewer. Review concrete intermediate commits while remaining construction continues. The lead may review others' small integration changes; dedicated reviewers own material release gates. A reviewer cannot sign off their own implementation package.
5. **Candidate:** combine the completed packages, run the free gate suite, freeze the candidate, and execute I once external prerequisites are met. Fix findings and rerun affected checks.

Prioritize work that unblocks a dependent package, then a reproduced release blocker, then independent ready work. Do not fill slots with speculative audits merely to keep agents busy. If integration is accumulating, stop dispatching another build and finish integration first.

## Ownership and isolation

The shell owner controls `web/src/App.tsx`, `main.tsx`, global styles, `package.json`, lockfile, and test configuration. Upload and answer builders own `web/src/intake/**` and `web/src/answer/**`. The library/API owner refreshes OpenAPI and generated types. The deployment owner controls workflow and deploy configuration. Provider-budget code, storage code, and their migrations receive explicit owners before implementation; migrations have one integrator-assigned ordering.

Each job has an input commit, owned paths, dependencies, acceptance commands, resource needs, checkpoint, and expected evidence. Crossing another job's owned paths requires an explicit handoff. A change to a shared contract invalidates affected fixtures and signoffs; the integrator owns propagation.

Each active builder uses an isolated branch/worktree. Every lane that runs a real worker receives a separate scratch database, staging directory, and ports. Never reuse the dogfood database for concurrent acceptance. A worker can claim another lane's jobs even when their code and ports differ. Frontend-only jobs use fixtures and need no database. One operator at a time controls each shared browser surface.

## Durable control state

GitHub owns obligations, dependencies, completion, and assignments. An ignored execution ledger stores only operational metadata: task ID, role, base commit, worktree, database/staging/port allocation, checkpoint, blocked reason, and handoff commit. The release manifest is derived from actual evidence. It does not become a competing task queue.

Task progression is `ready → assigned → implemented → independently checked → integrated → accepted`. On a failed check, return the task to the implementation owner with the reproduction. A merged branch does not by itself satisfy acceptance.

On restart, the lead reconciles the ledger against Git, the board, running workers, and saved evidence before dispatching. A missed checkpoint prompts inspection, not immediate termination. Preserve the last good commit. Check whether a side effect already happened before retrying any uncertain external operation.

## Governance authority and stopping rules

Governors run after relevant changes: a blocker, repeated failure, ownership conflict, integrated candidate, or milestone. They inspect changes since their last checkpoint. Every finding contains evidence, consequence, owner, and one concrete next action.

Delivery governance can reconcile unambiguous metadata and propose reassignment. Evidence governance can invalidate stale signoffs and request missing checks. Improvement governance can file a bounded repair with a reproduction and acceptance criterion. **Only the lead allocates execution slots and grants path ownership**, including a repair proposed by a governor. Explicit delegated leases remain subject to the same global capacity. The lead owns priorities, architectural decisions, scope changes, and release readiness. Reviewers provide independent evidence; governors cannot approve their own exceptions or weaken a release gate to create a pass.

Only one process-improvement task runs at a time. It must unblock work or remove a demonstrated recurring failure, with a measurable completion condition. Cosmetic improvements and speculative infrastructure go to the deferred queue. No governor creates another governor or an unbounded review chain.

Review the design once, revise it, and have fresh reviewers examine the revision. Continue only for unresolved concrete blockers. During implementation, use early contract review and one integrated-candidate acceptance pass, then targeted repair/retest. A second unsuccessful repair escalates the diagnosis to the lead; it does not waive the defect or force unsafe scope reduction.

After release, the same responsibilities can inspect service availability, stuck ingestion, access failures, budget stops, and regressions. Recurring monitoring and automated production changes require an explicit activation scope; this design does not create a running monitor. A future monitor stays quiet while state is healthy and unchanged.

## Critical implementation contracts

**Catalog and UI:** `GET /classes` returns class IDs and names. `GET /classes/{class_id}/files` returns file IDs, filenames, status, failed reason, and the existing user remedy. Reads are owner-scoped; missing and foreign classes use the same not-found treatment. Empty lists are successful. The browser restores state from the server and ignores replies from a previously selected class or request. An accepted upload is not a ready file; status polling supplies the truth.

**Identity:** use the configured project's verified token and an explicit allowed subject. Verify issuer, audience, expiry, signature and the allowed signing algorithm using a maintained library or the provider's verification endpoint appropriate to the project's signing-key mode. Reject arbitrary forwarded identity headers. Map the allowed Supabase subject to the configured existing GCT owner (currently `nate-dogfood` when preserving that corpus); never silently replace stored owner IDs with the Auth UUID. Missing configuration, synthetic auth, or a local development bypass must fail closed in a production build. The only public surfaces are generic sign-in/static assets and a minimal non-sensitive health response. Protect or disable API documentation/schema endpoints in production. Logout clears local course/answer state; course API responses are not publicly cached.

Revoked sessions can leave an issued access token valid until expiry. Declare that bounded lifetime explicitly, test it, and provide an immediate server-side owner-disable or reject-before control for emergency revocation. Do not claim instant global logout from signature verification alone. A checksummed auth configuration and its revocation policy belong to the release evidence.

**Private data:** disable the unused Supabase Data API and verify its REST/GraphQL paths cannot read the tables while Auth login and refresh still work. Storage objects are private and accessed server-side. Verify browser Storage list, read, write, delete, and signed-URL creation all fail, including with the allowed owner's ordinary Auth token. Do not put service keys, database credentials, documents, or session tokens in browser bundles, URLs, public Git, build contexts, or routine logs. Approved private corpus evidence remains in ignored local storage or a controlled private destination; release records use opaque IDs and hashes.

**Storage:** durably store complete immutable bytes before enqueueing. Store `reference` and original `filename` separately. Materialize under the existing worker lease/heartbeat, keep the original basename for parsing/citations, and clean worker temporary files. Classify transient/missing-object failures. Record object-upload/DB-enqueue orphans for bounded cleanup. Existing atomic publication and claim guards remain in force. An accepted file must remain queryable after restarts and redeployment.

**Spending:** reserve a conservative upper bound transactionally before any paid embedding/generation attempt. Account for input tokens, enforced model-specific output caps, chunk batches, grounding retries, and worker retries. Disable hidden SDK retries and route any deliberate retry through admission. Unknown model/pricing configuration fails closed. Atomically maintain `settled charges + unresolved reservations <= allowance` across API and worker; do not rely on process-local counters. Ambiguous timeouts and process death after dispatch retain their reservations until safely reconciled. Reaching a ceiling stops provider calls and produces a clear budget-blocked state with an explicit resume path; workers do not exhaust retry attempts by hammering a closed allowance. Include scheduled/live CI provider usage in an agreed separate allowance or the aggregate policy. This ceiling controls calls through GCT; it does not claim to cap unrelated use of the same external account. Price/version assumptions are part of release configuration. Test the actual SDK through a recording HTTP transport to prove request bounds and retry counts without spending; a fake `Generator` alone cannot establish this boundary.

**Deployment:** one source revision, image/build identifier, and compatible configuration identify the candidate. Run migrations once through the documented local/operator runner, which checks target identity and uses an exclusive migration lock. Prefer additive, backward-compatible migrations during the pilot. Reverting an image does not revert stored data. API and worker roll together through the supervised image. Keep a previous usable candidate, a private database export, and an object inventory plus bytes; rehearse recovery in a disposable environment. Free Supabase does not include automatic backups, and a database backup does not include Storage object bytes. A health response alone does not prove a working worker or grounding pipeline.

**Sleeping runtime:** H owns process supervision, readiness, signal forwarding, child reaping, and measured resource limits. F/G own interrupted materialization, claim recovery, and retained spend reservations. C/E own waiting, resuming, and actionable failure presentation. Current worker defaults include a 15-minute lease, two-second empty polling, and five attempts; these are not a seamless-wake policy. A specifies the recovery target; H tests it and measures/tunes lease, heartbeat, and empty-poll backoff against it. Test repeated interruption and report terminal failure honestly. Verify Supabase connectivity with TLS and the chosen direct/session-pooler mode; do not assume free IPv4 direct connectivity. Measure idle polling and object traffic rather than claiming the free tier can run indefinitely.

## Two distinct acceptance milestones

**Local candidate ready:** B–H can complete their local construction with scratch Postgres, storage/auth fixtures, fake providers, a built image, and constrained resource/recovery tests. The manifest records local image/schema/configuration IDs and marks hosted/live-provider evidence pending. Cloud credentials are not prerequisites for this milestone.

**Hosted release accepted:** I adds the actual deployed revision, account configuration, real sign-in/storage/database behavior, actual sleep/wake, and authorized real-provider evidence. A local pass cannot be relabeled a hosted pass. If live provider allowance remains zero, the available deliverable is an explicitly labeled fixture demonstration plus the ready integration; it is not a completed live tutor.

## Release gates and evidence

| Gate | Required evidence | Blocking rule |
| --- | --- | --- |
| Build and contracts | Python lint/format/non-live suite, database tests without unexplained skips, frontend type/lint/format/tests/build, schema contract checks on the combined revision | Required failing or unexecuted check blocks acceptance. |
| Primary journeys | Hosted create/reopen class, PDF/PPTX upload, queued/processing/ready/failed, ask, answer/refusal, refresh, desktop and phone | Broken required flow or stale cross-class answer blocks. |
| Answer presentation | Deterministic fixtures for GROUNDED, PARTIAL, REFUSAL, INTEGRITY_FLAGGED and transport ERROR; only server-resolved labels become citations | Flagged content presented as clean, invented citations, or lost warnings block. |
| Grounding behavior | Freeze a small acceptance bank before tuning: supported, unsupported, partial-support, wrong-class, and instruction-shaped-source cases. Record source IDs/pages, retrieved passages, actual answers, and independent claim-to-passage review | Demonstrated unsupported substantive claims presented cleanly, fabricated source support, or wrong-class leakage block the affected flow. Refusing a partially supported question can be recorded as a capability limit; do not require a fabricated PARTIAL response. |
| Access and privacy | Owner succeeds and old owner-scoped material remains visible; invalid/revoked identities obey the declared policy; raw-origin bypass and Data API access fail; browser Storage list/read/write/delete/sign fail; Auth refresh works with Data API disabled; inspect bundle and logs | Any demonstrated protected-data exposure, secret exposure, or unauthorized paid operation blocks. |
| Budget and request bounds | Concurrent API/worker admission; process death after dispatch; retries; ambiguous timeout; exhausted allowance; authoritative upload-byte bound; actual SDK recording-transport tests; rejected-request provider-call counts | Any call outside the configured reservation policy, breach of the durable allowance invariant, or unbounded paid attempt blocks. |
| Recovery and free capacity | Separate/disposable filesystems, worker death during materialization/indexing, DB reconnect, whole-container stop, actual hosted idle sleep/wake, second-connection publication, disposable restore/rollback, 512 MB/0.1 CPU stress and traffic measurement | Lost accepted material, partial publication, dead-worker false health, or inability to meet the declared pilot recovery/resource limits blocks the proposed topology. |

Fake providers prove wiring, state handling, and admission behavior. Actual provider runs and source comparisons establish bounded grounding evidence. The existing historical 12-question suite is a useful baseline, not a universal quality measurement. The release manifest states the observed limitations; it does not claim the V3 numerical bars are met.

Every signoff names the source commit, local image/build ID, schema/migrations, prompt/model and spend configuration, corpus/fixture version, checks actually run, evidence location, and open nonblocking issues. Hosted signoffs additionally name the real deployment ID; local signoffs explicitly say hosted evidence is pending. Changes invalidate affected signoffs. Structural checks or several agents agreeing cannot substitute for source verification. No builder provides the sole signoff for its own material change.

## External prerequisites and launch steps

The build and free checks can proceed while these are collected in one setup batch:

- Free Render and Supabase account access, actual quotas, supported connection mode, and region compatibility.
- A verified $0 infrastructure configuration without automatic paid upgrades or payment-backed overages.
- Runtime provider credentials and separate live-test and ongoing API allowances.
- The permitted Supabase subject and a working sign-in/recovery path.
- Authorization for the concrete provisioning, deployment, and final external actions when requested.

Nathan reported that he has never used Supabase and asked for a free option, including whether Vercel could be used. Free hosting is now the design preference. Account availability, provider credential validity, runtime API allowance, and final deployment authority remain unverified. Do not invent a dollar allowance from the Codex subscription. Account-dependent fields stay unresolved until supplied or verified; elapsed time is not approval.

1. Prepare the implementation and exact deployable candidate locally with free checks.
2. Verify the external prerequisites, free-plan configuration, and successful capacity prototype. Present any required paid alternative before committing spending.
3. Provision/configure the owner identity, private storage, database, server secrets, and spending ceilings within granted authority.
4. Apply the explicit migration step and deploy the matching API/worker candidate.
5. Run hosted acceptance and the authorized real-material demonstration; repair and retest affected gates.
6. Record the release manifest, private URL, operating instructions, limitations, and rollback path. Treat this tested revision as the private launch.

A provisional planning range is several focused working days after accounts and budgets are available. Re-estimate from the first integrated vertical slice and storage/budget prototypes; do not promise a calendar deadline from agent count. The critical path is contracts → usable integrated flow and durable guarded runtime → exact hosted acceptance.

## Design review record

Six specialist agents reviewed the design in two rounds, followed by a focused recheck of the revisions. They inspected the plan and relevant repository/provider evidence; these reviews did not execute product acceptance tests or deploy the app.

| Review | Changes incorporated |
| --- | --- |
| Delivery, assurance/governance, and operations | Replace permanent-agent overhead with a specialist roster; freeze contracts; isolate worker databases; require independent evidence; bound repair loops; identify hosting, identity, and spending prerequisites. |
| Fresh fleet, free-hosting, and security reviewers | Give E a dedicated builder and reserve review slots; make A executable; keep dispatch authority singular; separate local from hosted acceptance; test constrained capacity, sleep, and supervision; preserve owner identity; define revocation; enforce provider bounds at the actual SDK boundary. |
| Focused final recheck by all three fresh reviewers | No remaining concrete design blocker to local implementation. Begin A and the early capacity prototype. Free hosting feasibility and actual hosted/live-provider acceptance remain unproven. |

Further broad design review is deferred. Resume review for a changed contract, measured prototype failure, or concrete implementation finding.

## Source and decision references

Repository sources inspected October 2, 2026: [README](../README.md), [handoff](HANDOFF.md), [requirements](requirements.md), [roadmap](roadmap.md), [client tooling](decisions/0032-client-tooling-vite-typescript-npm.md), [hosting decision](decisions/0006-tech-stack-rationale.md), [storage transition](decisions/0010-v1-file-staging-vs-v2-object-storage.md), [queue decision](decisions/0011-async-substrate-db-backed-worker.md), and [grounding findings](../eval/FINDINGS.md). Amend ADR 0004/0006 for the limited early sign-in/same-origin release, reconcile the queue wording, and update N15's hard-cap assumption during A. Preserve existing contracts unless the amendment explicitly changes them.

Primary provider documentation checked October 2, 2026:

- [Render web services](https://render.com/docs/web-services), [free service limits](https://render.com/docs/free), [compute plans](https://render.com/docs/compute-plans), [deploy steps](https://render.com/docs/deploys), and [Docker process management](https://docs.docker.com/engine/containers/multi-service_container/).
- [Supabase Auth configuration](https://supabase.com/docs/guides/auth/general-configuration), [JWT verification](https://supabase.com/docs/guides/auth/jwts), and [session lifecycle](https://supabase.com/docs/guides/auth/sessions).
- [Supabase data access security](https://supabase.com/docs/guides/database/secure-data), [disabling the Data API](https://supabase.com/docs/guides/api/securing-your-api), and [Storage access control](https://supabase.com/docs/guides/storage/security/access-control).
- [Supabase pricing](https://supabase.com/pricing), [free project pausing](https://supabase.com/docs/guides/platform/free-project-pausing), [connection modes](https://supabase.com/docs/guides/database/connecting-to-postgres), and [backup scope](https://supabase.com/docs/guides/platform/backups).
- [Vercel Hobby](https://vercel.com/docs/plans/hobby) and [function limits](https://vercel.com/docs/functions/limitations).
- [OpenAI rate limits and spend alerts](https://developers.openai.com/api/docs/guides/terraform/rate-limits-and-spend) and [spending-controller design](https://developers.openai.com/cookbook/articles/per_run_spending_controller_responses_api). Alerts do not enforce a spending ceiling.
