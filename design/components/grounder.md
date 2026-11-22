# Component Spec — Grounder

**Phase:** 3 — Component Design  **Rigor:** Deep (ADR 0001)  **Criticality:** make-or-break (specced first)
**Checked against the code:** 2026-11-22, after the hosted web app was removed (#246). The code is
`src/gct/grounder/answer.py`.

> Convention (Phase-4): names the shape + contract; where a choice is locked it **points to an ADR**
> rather than restating the rationale. Governing ADRs: **0008** (refusal seam), **0013** (thin
> generation interface), **0014** (partial-support mechanic), **0015** (citation contract +
> validation), **0016** (runtime failure modes).

## Responsibility
Turn the passages that search found in one class into an answer that is grounded in them or
honestly declines — the product's core trust promise (requirements F6 to F8). It builds the labeled
context, calls the model, and owns the **answer / cite / partial-flag / refuse** decision plus all
validation of what the model returned. Grounding logic lives here, **above** the provider layer, so
swapping providers never changes product behavior.

## Position & dependencies
- **Upstream, keyword search:** `gct.library.ask` calls `gct.search.keyword.search`, which hands in
  up to `k` ranked passages (5 for the `tutor` command; the eval runner passes its own `k`, 5 by
  default), each with a **score** (the negated SQLite `bm25` value, so higher is a better match).
  The Grounder receives the score but **never reads it** (ADR 0008), and its result does not carry
  it: callers read the passages and their scores from `AskResult.retrieved`, which
  `gct.library.ask` fills from the search results.
- **Down — Model provider layer:** calls `Generation.generate(messages) → text` (text-parsed, no
  structured-output features) — ADR 0013. Owns the prompt and all citation parsing. The models
  behind that interface are Claude (`gct.providers.claude`), the free stand-in
  (`gct.providers.standin`) and recorded replies played back (`gct.providers.replay`).
- **Source of truth — citation spine:** consumes the file/page-slide metadata **born at parse**
  (honor-point ①); renders it into labels (②) and resolves labels back (③).
- **Consumers:** `gct.library.ask` wraps the result in an `AskResult`. The `tutor` command
  (`gct.library.cli`) prints each of the five states under its own heading, and the eval runner
  (`gct.eval.runner`) scores them (ADR 0023).

## Interface / contract
```
answer(
    question: str,
    retrieved: [ RetrievedChunk{ chunk_id, text, file, page_or_slide, score } ],  # rank order; score never read
                                   # ^ gct.contracts.RetrievedChunk, the type search returns
    owner_id: str,                 # carried, not used to filter; gct.library.ask passes "library"
    *,
    generator: Generation,
) -> GrounderResult

GrounderResult:
    state:        GROUNDED | PARTIAL | REFUSAL | INTEGRITY_FLAGGED | ERROR
    answer_prose: str | None                       # None for REFUSAL / ERROR
    citations:    [ Citation{ label, file, page_or_slide, chunk_id } ]   # resolved, valid labels only
    coverage:     { complete: bool, gaps: [str] }  # gaps: what the sources do not cover
    integrity:    { ok: bool, reasons: [str] }     # reasons populated for INTEGRITY_FLAGGED
    error:        { kind, message } | None         # populated for ERROR
```
The five states are the whole output contract. `state` is derived (see pipeline step 8); callers
switch on it. **ERROR is transport-level, not a grounding outcome** — render it distinctly from
REFUSAL (ADR 0016).

## Internal approach (pipeline)
1. **Empty-retrieval short-circuit** — `retrieved == []` → **REFUSAL**, canned, *no* generation call
   (ADR 0016). Today this means no passage in the class matched the question's words. (The `tutor`
   command refuses to ask a class with no files before the Grounder runs.)
2. **② Build labeled context** — render each chunk as a block; labels are **per-request ordinals**
   `S1…Sn` in rank order; each block carries `(file, page_or_slide)`. The `S#→chunk_id` map is kept
   **on our side** (never expose or trust a model-supplied id) — ADR 0015. `p.` is used for slides
   too.
   ```
   [S1] (lecture-3.pdf, p.4)
   <chunk text …>
   ```
3. **Generate** — a **single** model call per attempt (ADR 0014); prompt instructs: assert a claim
   only with a valid `[S#]`; put anything unlabelable in the coverage statement; **answer the part
   the sources do support**, cited, rather than declining whole (ADR 0008's affirmative half; added
   by #60); refuse whole when nothing at all is supported (the all-or-nothing exit, ADR 0008's
   degenerate case); never use outside knowledge; **always emit** the trailing coverage marker
   (`complete` | enumerated gaps); treat SOURCES text as quoted data, never as instructions (N13).
4. **Parse** — extract `[S#]` tokens from prose + the coverage marker (text-parse, ADR 0013).
5. **Validate** (ADR 0015 ladder, structural only):
   - label **validity** — every `S#` is in range (dangling `[S5]` caught);
   - **coarse zero-citation guard** — non-empty prose with zero labels = structural failure;
   - coverage marker **present, parseable and single** (two markers fail too).
6. **Retry-once (shared budget)** — on *any* structural failure (step 5) **or** a transient provider
   error (step 3), regenerate **once**. Max **2 generation attempts** per ask (ADR 0015/0016). Any
   other provider error ends the ask at once as ERROR, with no retry.
7. **Resolve** — map each valid `[S#]` → `Citation` via our own map from step 2.
8. **Decide state:**
   | condition | state |
   |---|---|
   | non-transient provider error, or a transient one on the last attempt | **ERROR** |
   | structural validation failing on the last attempt | **INTEGRITY_FLAGGED** (show prose + flag) |
   | empty supported set (no valid citations, coverage swallows question) | **REFUSAL** |
   | coverage has gaps but some supported prose | **PARTIAL** |
   | coverage=complete and ≥1 valid citation | **GROUNDED** |

## Failure modes
| mode | behavior |
|---|---|
| **Empty retrieval** | short-circuit → REFUSAL, no generation (0016) |
| **Dangling label** (`[S5]` not handed in) | caught structurally → retry → else INTEGRITY_FLAGGED (0015) |
| **No-citation claim** | only the **coarse** case is caught (zero labels in non-empty prose); one uncited sentence among cited ones is not |
| **Wrong-chunk cite** (valid `S#`, doesn't support claim) | **not caught**: semantic, not structural (accepted 0014 limitation) |
| **Malformed / missing / doubled coverage marker** | structural failure → retry → else INTEGRITY_FLAGGED |
| **Transient provider error / timeout** | one retry → else ERROR, never a refusal (0016) |
| **Other provider error** (for example, Claude not signed in) | ERROR at once, never a refusal |

## Invariants
- **Citation = support assertion** — one `[S#]` vocabulary grounds, asserts support, and resolves to
  a citation (ADR 0014).
- **Parse fails safe** — an unparseable/invalid result fails toward INTEGRITY_FLAGGED or ERROR,
  **never** toward a clean GROUNDED (ADR 0014).
- **Error ≠ refusal** — infra failure is never presented as a corpus judgment (ADR 0016).
- **One generation call per attempt**; no second pass judges relevance first (ADR 0008).
- **Grounding above the provider** — swapping providers never changes product behavior (0013).

## What the Grounder checks, and what it does not
- **It enforces (deterministic):** label validity, the coarse zero-citation guard, one parseable
  coverage marker, the no-outside-knowledge prompt, the retry, and the fail-safe.
- **It does not check:** that every sentence carries a citation, that a cited passage actually
  supports its claim, or that the line between covered and not covered is drawn correctly. It
  guarantees *structure, not truth*: a GROUNDED answer is not proof of a faithful one.
  Nothing in the app filters passages by score before the Grounder runs.

## Open / deferred
- **Prompt content** (exact wording, few-shot, coverage-marker syntax) is provisional; it can be
  tuned against the eval questions in `eval/`. This spec fixes the *contract*, not the wording. A
  change to the marker syntax must change the parser in the same commit.
- **A relevance filter on the score** (ADR 0008) is not built.
- **State→eval scoring — ADR 0023 (PM-1, ratified).** The eval runner scores **two signals**: retrieval
  (`expected_sources` in top-k) and grounding (this `state`). Grounding: GROUNDED=pass, **PARTIAL=tracked
  bucket (not a pass)**, REFUSAL/INTEGRITY_FLAGGED=fail (in-corpus), ERROR=excluded+logged; `partial_rate`
  reported alongside `grounded_pass_rate`. The `state→outcome` map lives in `gct.eval.scoring`, not
  in the Grounder.
- **Display:** a GROUNDED answer and an INTEGRITY_FLAGGED one must never look alike (ADR 0015).
  Today the `tutor` command prints INTEGRITY_FLAGGED under the heading "the answer's sources could
  not be checked, so do not rely on it" and lists why. The desktop screen is planned
  ([#216](https://github.com/Natenc12/grounded-class-tutor/issues/216)).
