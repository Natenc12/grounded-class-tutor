# Saved connection recovery

This work addresses unnecessary ChatGPT login instructions after a local credential
storage failure. It does not bypass macOS Keychain permission or replace encrypted
storage. The existing app name, user-data directory and encryption provider remain
unchanged. No money, live generation, account inspection or Keychain modification
is required for the automated checks.

## Observed problem

At `c1368cc`, the SDK's `getSession()` converted every saved-store read failure into
`reauth_required`. The renderer consequently offered **Reconnect with ChatGPT**
even when the encrypted credentials were intact and only OS access was temporarily
unavailable. There was no separate action to retry the saved connection. Startup
also initially exposed disconnected controls while restoration was still pending.
A failed model-catalog request left a connected account with no visible retry.

The correction distinguishes initial restoration, unavailable saved storage, an
existing connected account and a genuinely revoked connection. Recovery retries
the saved session and model catalog; it does not start browser authorization.
Credentials remain encrypted and are preserved on failed reads. New authorization
continues to require an explicit sign-in action.

## Automated verification

The full desktop suite passes **183 tests**. Its 19 independently authored recovery
cases cover repeated fresh client instances, unchanged encrypted credential bytes
and host identity, unavailable/denied storage, expired access without automatic
OAuth, genuine revocation, startup ordering, model-catalog recovery, strict IPC,
cancellation, shutdown and renderer action states. Additional implementation tests
check stale-error clearing and that a denied credential read is attempted only once.
Existing authorization, refresh and encrypted-storage regression tests also pass.

The updated bundle passes the relocated packaged-runtime smoke, including 76
native-library link checks and mocked generation. It was built under ignored
`desktop/build/session-recovery-dist` so the currently open app in `desktop/dist`
was not overwritten. The ZIP is 215,486,358 bytes with SHA-256
`f7115184f32b986300a35d8d8f1cafda06cfa898bef4b054010e29ffc82c7300`.

Cancellation suppresses late results and cancels model-catalog requests. The
existing synchronous encryption provider can wait on a native macOS dialog;
the app's cancel action cannot dismiss that protected dialog. Closing still waits
for in-flight credential work instead of abandoning a write.

## macOS permission and builds

The previous and current pre-fix previews have the same bundle identifier but
different ad-hoc designated requirements. Read-only `codesign -d -r-` inspection
found `bf59f503ffe5454d98eb843a17e5823744b0ce30` in the older preview and
`22730a1fba1f1be4b8724c061b427642c3368928` in the semantic-search preview.

An independent disposable test copied `/usr/bin/true` into a synthetic app bundle,
signed it with `codesign --force --sign -`, and checked it with
`codesign --verify --strict`. Changing only a bundled resource and signing again
changed its designated requirement. Re-signing identical bytes preserved the
requirement. No synthetic executable was launched and no Keychain was queried.
A stable installation path by itself cannot prevent identity changes on updates.

For the same app identity, a successful **Always Allow** normally persists access
to the named Keychain item. A changed preview build may require another permission
grant. This behavior is documented by [Apple](https://support.apple.com/guide/keychain-access/kyca1243/mac)
and [Electron](https://www.electronjs.org/docs/latest/api/safe-storage).

A persistent personal self-signed identity is a possible free route to stable
build identity; [Apple documents it for internal development](https://developer.apple.com/library/archive/documentation/Security/Conceptual/CodeSigningGuide/Procedures/Procedures.html).
That setup and native update testing are not implemented or claimed by this fix.
Do not substitute an identifier-only requirement, broad Keychain access, plaintext
credentials, deletion of an existing item or a Keychain reset.

## Native acceptance still required

Before replacing the preview, its UI displayed the saved account as connected,
with the existing library and model catalog. A normal quit followed by reopening
the same build reached the startup UI, but the next accessibility read timed out.
The user was asked to complete a Keychain prompt if present. That timeout alone
does not establish its cause or prove a successful relaunch. No password/token
contents were inspected and no tutor question was sent.

With the final build, the user must complete any macOS permission prompt for GCT.
Then verify that a normal quit and two relaunches preserve the connected account
without browser login or another Keychain prompt. Separately, a future stable
signing solution must pass the same check after an actual changed build.
Synthetic tests cannot prove native permission persistence.
