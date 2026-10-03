# Local GCT subscription proof

Nathan selected a local, downloadable direction on October 2, 2026. The immediate
experiment is GCT's own ChatGPT sign-in followed by one answer with source
citations. This replaces hosted deployment as the next milestone; the earlier
Render/Supabase launch proposal is historical planning, not the current release gate.

## Scope

The `desktop/` preview uses the official Sign in with ChatGPT local SDK, Electron,
and GCT's existing Python parser, chunker, and Grounder. It reads a selected PDF or
PPTX locally, then supplies up to five explicitly selected pages to generation.
The sample is synthetic study-method material, clearly labeled in the UI.

This is a proof of the account integration and citation path. It does not replace
the production vector retriever, ingestion queue, database, or existing API. It
does not claim semantic retrieval or automatic factual verification: the existing
Grounder validates citation structure, while a reviewer must compare claims to
their cited passages. No change to a production ADR is implied by this spike.

The SDK uses the user's explicitly authorized ChatGPT plan. It has no API-key
fallback and this path makes no embeddings calls. Originals stay on the laptop;
the selected text and question go to OpenAI when Ask is pressed. Complete model
responses pass through the Grounder before display. Partial streams, cancellation,
or account errors cannot be presented as completed answers. A structural repair
can use a second generation attempt under the existing Grounder policy.

The preview does not promise a hard token ceiling: this sign-in route currently
does not support `max_output_tokens`. App limits and permission to use paid credits
are controlled in ChatGPT Settings → Usage. For a no-additional-spend trial, leave
credit usage disabled. No live inference runs automatically at launch or sign-in.

## Run locally

From this checkout, run `uv sync --extra dev --locked`, then in `desktop/` run
`npm ci` and `npm start`. Node 22.12 or newer is required. The Python environment
is found in the checkout's `.venv`; `GCT_PYTHON` can select an explicit interpreter.
No database or `.env` is needed for this proof.

The app opens its own browser sign-in only when Continue with ChatGPT is pressed.
Credentials stay encrypted through the OS secret store, in the app's data folder.
The renderer gets account labels and narrow actions, never tokens or file paths.
The Python subprocess gets document data and messages, never provider credentials.
The selected source is copied into private app-owned staging so preview and Ask
use the same bytes. Failed replacements remove their candidate and retain the
previous source; successful replacements remove the old copy. Normal exit cancels work, waits for in-flight SDK operations to finish saving any
credential rotation, and waits for parser/copy cleanup, and the next launch recovers abandoned staging run
folders after acquiring the single-instance lock. Originals are never changed.
Sources and answers are not yet saved as a persistent class library.

Compressed files also have expansion limits: PPTX accepts at most 5,000 archive
parts, 32 MiB per part and 64 MiB in total; PDF decoding is bounded to 16 MiB per
stream and 64 MiB cumulatively. These are parser budgets, not an OS filesystem or
memory sandbox. The dedicated process deadline and forced termination remain
necessary. The PDF guard relies on the pinned pypdf version and restores its
temporary decoder settings for in-process tests.

## Evidence required

Automated checks must prove Python citation resolution, safe failure handling,
input bounds, complete-response handling, credential isolation, and SDK auth/
storage behavior using mocks. A native UI check proves the window starts and the
sample loads. These do not establish account eligibility or live inference.

The live proof requires Nathan to complete GCT's own sign-in and plan-use consent,
choose an available model, and ask a sample question. Record only a redacted
result and source/citation comparison. Then repeat with selected course material
and an unsupported question. Do not call this a shipped tutor until the local
library, search quality, recovery, installer, and update/backup behavior are tested.

## Next architecture decisions

After the account proof succeeds, preserve the independent builder/reviewer and
single-dispatcher rules from the fleet design. Replace hosting work with desktop
packaging, local persistence/search, and account/usage integration. Choose local
embeddings or lexical search through a measured retrieval test; the existing
1536-dimension database and model stamps require explicit migration/reindexing if
the embedder changes. Do not silently swap SQLite for the existing Postgres queue.

## Observed proof, October 2, 2026

The native window opened, Nathan completed GCT's own OpenAI sign-in and plan-use
consent, and the account's model catalog loaded. Two authorized questions then
completed using GPT-6-Astra and the synthetic sample:

- "What is active recall, and how does spaced practice work?" returned GROUNDED.
  The answer correctly described memory retrieval and study sessions spread over
  time. Both claims cited S1, `local-proof-sample.pdf`, page 1, and were manually
  compared with that passage.
- "According to this material, exactly how many days should separate study
  sessions?" returned REFUSAL. The UI displayed "Not supported" and identified the
  missing exact interval instead of inventing one.

This establishes the integration for this account and these sample questions;
it does not establish universal account eligibility or retrieval quality. No API
key, embeddings service, or database was used. Account identifiers and credentials
are excluded from this record. A real course-file acceptance check, persistent
class library, and standalone installer remain unverified.

After the adversarial repairs and migration back to the original checkout, a
new native run repeated the active-recall/spaced-practice question successfully
through GPT-6-Astra. Both definitions matched the displayed synthetic passage and
cited S1, `local-proof-sample.pdf`, page 1. This final compatibility check used the
patched completion adapter and preserved the existing account connection.

## Adversarial review

A separate systems architect and testers reviewed parser inputs, account state,
provider streams, bridge messages, file ownership and shutdown. Confirmed defects
were reproduced before repair: omitted PowerPoint tables, excessive archive
expansion, huge citation ordinals, inconsistent completion payloads, hidden
credential-storage errors, stale model catalogs, document-process exit during
generation, accumulated staging copies, and a FIFO that could block selection.
The regression suites use synthetic fixtures and mocked account/network traffic.

The desktop now checks bridge shapes and grounding-state consistency, while the
Python Grounder still owns the underlying decision. Neither check establishes
that a correctly formatted citation entails a claim. The architect reproduced
that documented limitation using a fake provider; factual accuracy and resistance
to instructions embedded in real course materials remain evaluation work.

The SDK has one documented local completion-validation patch; see the vendor
README. This account route can return an explicit completed response with an empty
output array after streaming the answer. The adapter accepts that observed form
only with nonempty streamed text; when final output is included, it must agree
with the stream. Contradictory, incomplete, and malformed completions still fail. Free desktop CI runs the build and mocked integration suite. Paid API
gates remain available only through an explicit manual opt-in. Remote CI success
and the narrow two-question live proof do not establish downloadable release
readiness or the deferred persistent-library architecture.

## Sources

- [Official integration cookbook](https://developers.openai.com/cookbook/articles/sign-in-with-chatgpt)
- [Sign-in contract](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [Preview limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
- [Account and usage controls](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- SDK version and license: `desktop/vendor/README.md`.
