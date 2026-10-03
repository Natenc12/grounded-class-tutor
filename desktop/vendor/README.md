# Pinned Sign in with ChatGPT SDK

`siwc-local/` contains the official `packages/local` source and tests from
https://github.com/openai/sign-in-with-chatgpt-devkit at
`f723814abdccec135b519c451fb6e1992ee5e933` (retrieved October 2, 2026).

The SDK is not published as an npm package. GCT uses this pinned workspace copy.
Upstream tests and package metadata are unchanged. A local hardening change in
`siwc-local/src/responses.ts` validates the completed response's status, error,
and output text against streamed deltas, and stops at the terminal event so late
deltas cannot alter the answer. This catches contradictory completion payloads,
missing or reordered text, and chunk-boundary-dependent output discovered by
GCT's adversarial tests. The direct sharing route was observed returning
`response.completed` with `status: "completed"` and `output: []` after streaming
its text. This sparse shape is accepted only with nonempty accumulated text;
missing/nonarray output, empty text, errors, or another status are rejected.
When the completion includes output, its text must exactly match the deltas.
A bare `response.completed` tag is insufficient. The root
`desktop/tsconfig.base.json` is also copied unchanged. GCT's build invokes the
compiler directly rather than the upstream repository's notice-copying script.
Upstream LICENSE and third-party notices remain alongside the vendored source.

GCT also modifies `siwc-local/src/index.ts` and `src/types.ts` so an unreadable
encrypted store reports `storage_unavailable`, separately from a saved revoked
connection that requires a fresh sign-in. A successful local session read clears
stale transient errors. These changes do not alter the credential format,
encryption provider, storage path, or token-refresh behavior. Added local tests
in `test/saved-session.test.mjs` and `test/session-recovery-adversarial.test.mjs`
use disposable synthetic credentials. Each modified SDK source file carries a
change notice; the modifications use the same license as the upstream SDK.
Request and sign-in failure handling uses the same storage-unavailable state
when credentials cannot be read, without immediately retrying a denied OS read.

This DevKit has a noncommercial license. This proof uses it for personal learning
and experimentation. Its license does not turn GCT's independently authored code
into SDK code, and does not grant permission for commercial distribution of the
DevKit. Review the preserved license before any distribution change.

Credentials are created only by GCT's own sign-in flow and are never copied from
Codex or another app. Tests use synthetic credentials and mocked network traffic.
