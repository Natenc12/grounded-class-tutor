# Local desktop migration validation

Observed October 2, 2026 in the original GCT checkout on macOS. This records the
local source refactor under ADR 0033, not a packaged installer release.

## Native application

- Existing encrypted ChatGPT connection reopened without changing its storage path.
- Created a class and imported a synthetic, one-page PDF through the native file dialog.
- Quit GCT, moved that synthetic original away, and relaunched it. The class and saved
  document remained available; the original was restored after the check.
- Asked across the class without reopening the source file. The connected GPT-6-Astra
  route returned a grounded active-recall/spaced-practice answer citing page 1.
- “View saved passage” displayed the matching text from the saved library.
- Created a backup through the native save dialog; reopening it recovered the same
  class/document and original bytes, with owner-only file permissions.
- Asked for the exact ideal practice interval, which the source explicitly does not
  prescribe. GCT displayed Not supported with that specific coverage gap.
- These two live requests used the authorized account connection and synthetic text.
  No paid API/embedding fallback or paid workflow was configured or dispatched.

## Automated failure coverage

The local suites exercise parser bounds, immutable import snapshots, real process
termination during import, atomic rollback, restart persistence, duplicate filenames,
class isolation, source identity, consistent backup, corrupt/newer database refusal,
FTS correspondence including NULL fields, and saved-original checksum validation.

The desktop suites exercise sender/argument validation, all three evidence scopes,
real Python service calls across fresh processes, safe text rendering, stale answer
and citation suppression, model/account changes, cancellation and actual child
reaping before staging cleanup, and encrypted credential shutdown ordering.

Reviewers reproduced and required fixes for logical search-index corruption, NULL
comparison bypass, stale source previews, ambiguous scope controls, and a citation
lookup completing after account invalidation. Final commit reviews and CI results
are recorded on the corresponding pull requests rather than asserted here as a
permanent test count.

## Retrieval measurement and remaining limits

A read-only probe used five existing private evaluation files, 122 chunks, and
12 stored questions. Expected sources appeared for 6/8 answerable questions at five
candidates and 8/8 at eight candidates. The full local parse/index/query probe took
approximately 0.8 seconds on this laptop. Four unsupported questions still produced
search candidates; the Grounder must decide support. No private corpus was committed.

This is a small retrieval baseline, not an accuracy guarantee. Keyword search can
miss paraphrases. Explicit-page mode supplies all chosen evidence or fails clearly
when the context limit is exceeded. Scanned documents still need extractable text;
math/table fidelity and a broader claim-support evaluation remain release work.
Standalone packaging, installation/upgrade testing, in-app restore, conversation
history, and the visual redesign are separate milestones.
