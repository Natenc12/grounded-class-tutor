# Private macOS app preview

The current build targets Apple Silicon (arm64) macOS. It includes Electron,
CPython 3.13.14, the GCT core, document parsers and their locked dependencies.
Running the resulting app needs no checkout, Python installation, Node, npm or
uv. Internet access and an eligible, personally connected ChatGPT account are
needed for tutor generation; the library and source preview work locally.

Build on an arm64 Mac with Node 22.12+ and uv available:

```sh
cd desktop
npm ci
npm run package:mac
```

The script downloads a pinned Python archive with a checked SHA-256, installs
runtime-only dependencies from `uv.lock`, builds the current GCT wheel, stages
an explicit desktop allowlist, signs the bundle ad hoc, verifies it, runs the
relocated-runtime smoke, and creates:

- `desktop/dist/Grounded Class Tutor-darwin-arm64/Grounded Class Tutor.app`
- `desktop/dist/Grounded-Class-Tutor-0.1.0-mac-arm64.zip`
- the adjacent `.zip.sha256` checksum

Extract the ZIP and move the app to your desired location, such as Applications.
The archive is a personal local preview: it is **not Developer ID signed or
notarized**, so macOS can warn or block it after download. There is no automatic
update or installer wizard. Do not label this a public production release or
promise clean-machine compatibility based only on the local smoke tests.
Neither building nor testing uploads artifacts or reads existing app data.
The first packaged launch may ask macOS Keychain permission to use the saved
connection; changing the application signature can change its access prompt.

`npm run test:packaged -- '/absolute/path/Grounded Class Tutor.app'` copies the
bundle to a temporary path containing spaces and a non-ASCII character. It runs
its real shipped Python and bridge outside the checkout, exercises PDF/PPTX,
SQLite FTS5, persistent import/reopen, synthetic generation, citation/deletion
and backup, and checks native-library links for developer-machine dependencies.
It uses synthetic data and no SDK account. Native Electron UI, account continuity
and real generation must be checked separately before handing over a release.
This is a sanitized-environment test on the build Mac, not a fresh-Mac test.

## Data and identity

The packaged app preserves the existing application name and
`~/Library/Application Support/Grounded Class Tutor Local Proof` data directory.
Credentials remain in its encrypted `chatgpt/` store, independently of the
`library.sqlite3` database. A failed OS credential decryption preserves saved
bytes and blocks sign-in from overwriting them. Packaging does not migrate,
copy, reset or bundle that data. Backups are ordinary unencrypted local library
snapshots and contain documents, but no ChatGPT credentials.

Packaged Python runs from `Contents/Resources/python` with `-I -B`, an explicit
working directory and a minimal environment. It ignores `GCT_PYTHON`, inherited
Python paths, user packages and API-key variables. A missing runtime fails
closed; it never falls back to a system interpreter. The existing source
`npm start` workflow retains its development interpreter selection.

## Licenses and provenance

`Contents/Resources/licenses` contains a build manifest, Electron/Chromium
license texts, and the exact standalone-Python archive's license texts and
`PYTHON.json` metadata. Python package notices remain in their `.dist-info`
directories. JavaScript dependency notices remain inside their directories in
`app.asar`; the SDK also includes its pinned source, LICENSE, third-party notices,
and GCT modification notes. The changed SDK source and compiled output both
carry a change notice. Packaging strips development tools, static archives and
bytecode from the Python runtime, but does not modify its interpreter or modules.

The vendored OpenAI SDK is licensed for noncommercial use; commercial rights
require separate permission under its license. Its license does not itself
grant service access. Each user connects their own eligible ChatGPT account.
GCT has no API-key or paid-provider fallback. Review both SDK and service terms
before widening distribution; this build is a private personal preview.

Build outputs and download caches are ignored under `desktop/build` and
`desktop/dist`; never commit or publish them incidentally with source changes.
