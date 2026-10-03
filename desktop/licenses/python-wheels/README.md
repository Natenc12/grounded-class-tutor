# Supplemental wheel notices

The exact macOS arm64 `tokenizers==0.23.2` and `flatbuffers==25.12.19` wheels do
not carry complete license texts. `THIRD_PARTY_NOTICES.txt` supplies original
texts from their pinned upstream source archives. `manifest.json` records each
source URL, archive checksum, original notice path, checksum and byte range.

The tokenizer wheel's upstream CycloneDX SBOM lists all targets. Its 127
components are preserved as a conservative superset, including build tools and
platform-specific dependencies; this is not a claim that every component is
linked into the Mac binary. The original SBOM remains in the installed wheel.
The manifest binds its exact bytes and both wheels' metadata. Packaging stops
if those identities change, so upgrading a wheel requires a new notice review.

All recognized license, notice, copyright and authors files are preserved.
Distinct leading copyright/license comments are also included, covering the
native esaxx and Oniguruma attributions and Unicode notices. Identical leading
comments within a source are included once. The defmt-parser crate omits its
license files; its `.cargo_vcs_info.json` identifies the upstream commit used
for the two parent repository license files. Original texts are unchanged;
section headings and this explanation are GCT additions.

Reproduce the text using free public downloads, without running source code:

```sh
uv run --no-sync python desktop/scripts/collect-python-notices.py \
  --cache .ship/python-notice-cache \
  --output .ship/reproduced-python-notices.txt
```

The collector verifies all archive and excerpt hashes, and then requires the
complete output to match the committed notice hash. It never extracts or
executes archive contents. Normal application builds use the verified committed
text and make no extra notice downloads. These build tools are not bundled in
the application.
