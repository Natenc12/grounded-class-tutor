# Component Spec — Grounder

**Phase:** 3 — Component Design  **Rigor:** Deep (ADR 0001)  **Criticality:** make-or-break (specced first)

> Convention (Phase-4): names the shape + contract; where a choice is locked it **points to an ADR**
> rather than restating the rationale. Governing ADRs: **0008** (refusal seam), **0013** (thin
> generation interface), **0014** (partial-support mechanic), **0015** (citation contract +
> validation), **0016** (runtime failure modes).

## Responsibility
Turn *this owner's* retrieved chunks into an answer that is grounded in them or honestly declines —
the product's core trust promise (F6–F8, N2–N4). It builds the labeled context, makes the single
model call, and owns the **answer / cite / partial-flag / refuse** decision plus all validation of
what the model returned. Grounding logic lives here, **above** the provider layer, so swapping
providers never changes product behavior.

## Position & dependencies
- **Upstream — Retriever:** hands in ranked chunks + similarity **scores**. The score is plumbed but
  **not gated** in V1 (the seam exists day one; the V3 relevance filter turns on here) — ADR 0008.
- **Down — Model provider layer:** calls `Generation.generate(messages) → text` (text-parsed, no
  structured-output features) — ADR 0013. Owns the prompt and all citation parsing.
- **Source of truth — citation spine:** consumes the file/page-slide metadata **born at parse**
  (honor-point ①); renders it into labels (②) and resolves labels back (③).
- **Consumers — API adapter / React SPA:** receive a `GrounderResult` and render its five states
  (N11 trust surface; ADR 0012).

## Interface / contract
```
Grounder.answer(
    question: str,
    retrieved: [ RetrievedChunk{ chunk_id, text, file, page_or_slide, score } ],  # rank order; score not gated in V1
                                   # ^ the Retriever's shipped return type (gct.retriever.retrieve,
                                   #   issue #5) — one name for one type across the seam.
    owner_id: OwnerId,
) -> GrounderResult

GrounderResult:
    state:        GROUNDED | PARTIAL | REFUSAL | INTEGRITY_FLAGGED | ERROR
    answer_prose: str | None                       # None for REFUSAL / ERROR
    citations:    [ Citation{ label, file, page_or_slide, chunk_id } ]   # resolved, valid labels only
    coverage:     { complete: bool, gaps: [str] }  # gaps populated for PARTIAL / REFUSAL
    integrity:    { ok: bool, reasons: [str] }     # reasons populated for INTEGRITY_FLAGGED
    error:        { kind, message } | None         # populated for ERROR
```
The five states are the whole output contract. `state` is derived (see pipeline step 8); callers
switch on it. **ERROR is transport-level, not a grounding outcome** — render it distinctly from
REFUSAL (ADR 0016).

## Internal approach (pipeline)
1. **Empty-retrieval short-circuit** — `retrieved == []` → **REFUSAL**, canned, *no* generation call
   (ADR 0016). In V1 this ≈ empty/un-ingested class.
2. **② Build labeled context** — render each chunk as a block; labels are **per-request ordinals**
   `S1…Sn` in rank order; each block carries `(file, page_or_slide)`. The `S#→chunk_id` map is kept
   **server-side** (never expose or trust a model-supplied id) — ADR 0015.
   ```
   [S1] (lecture-3.pdf, p.4)
   <chunk text …>
   ```
3. **Generate** — a **single** model call (ADR 0014); prompt instructs: assert a claim only with a
   valid `[S#]`; put anything unlabelable in the coverage statement; never use outside knowledge;
   **always emit** the trailing coverage marker (`complete` | enumerated gaps).
4. **Parse** — extract `[S#]` tokens from prose + the coverage marker (text-parse, ADR 0013).
5. **Validate** (ADR 0015 ladder, V1-structural only):
   - label **validity** — every `S#` is in range (dangling `[S5]` caught);
   - **coarse zero-citation guard** — non-empty prose with zero labels = structural failure;
   - coverage marker **present + parseable**.
6. **Retry-once (shared budget)** — on *any* structural failure (step 5) **or** a transient provider
   error (step 3), regenerate **once**. Max **2 generation attempts** per ask (ADR 0015/0016).
7. **Resolve** — map each valid `[S#]` → `Citation` via the server-side map.
8. **Decide state:**
   | condition | state |
   |---|---|
   | provider error persists after retry | **ERROR** |
   | structural validation still failing after retry | **INTEGRITY_FLAGGED** (show prose + flag) |
   | empty supported set (no valid citations, coverage swallows question) | **REFUSAL** |
   | coverage has gaps but some supported prose | **PARTIAL** |
   | coverage=complete and ≥1 valid citation | **GROUNDED** |

## Failure modes
| mode | V1 behavior |
|---|---|
| **Empty retrieval** | short-circuit → REFUSAL, no generation (0016) |
| **Dangling label** (`[S5]` not handed in) | caught structurally → retry → else INTEGRITY_FLAGGED (0015) |
| **No-citation claim** | only **coarse** case caught (zero labels in non-empty prose); per-claim = V3 |
| **Wrong-chunk cite** (valid `S#`, doesn't support claim) | **invisible in V1** — semantic, N4/V3 (accepted 0014 limitation) |
| **Malformed / missing coverage marker** | structural failure → retry → else INTEGRITY_FLAGGED |
| **Provider error / timeout** | one retry → else ERROR, never a refusal (0016) |

## Invariants
- **Citation = support assertion** — one `[S#]` vocabulary grounds, asserts support, and resolves to
  a citation (ADR 0014).
- **Parse fails safe** — an unparseable/invalid result fails toward INTEGRITY_FLAGGED or ERROR,
  **never** toward a clean GROUNDED (ADR 0014).
- **Error ≠ refusal** — infra failure is never presented as a corpus judgment (ADR 0016).
- **Single generation call in V1**; the two-pass per-chunk relevance filter is the V3 turn-on (0008).
- **Grounding above the provider** — swapping providers never changes product behavior (0013).

## The V1-structural / V3-semantic line (what this box does *not* do in V1)
- **V1 enforces (deterministic):** label validity, coarse zero-citation guard, coverage-marker
  presence, no-outside-knowledge prompt, retry, fail-safe.
- **V3 measures (empirical, elsewhere):** per-claim citation presence, claim↔chunk **entailment**
  (N2/N4), boundary **correctness** (N3), and the calibrated score-filter. The Grounder does **not**
  verify entailment in V1 — it guarantees *structure, not truth*. A green V1 is not "faithful."

## Open / deferred (out of this spec)
- **Prompt content** (exact wording, few-shot, coverage-marker syntax) — an **empirical spike**
  deliverable, tuned against the eval set; the spec fixes the *contract*, not the prose.
- **V3 relevance filter** calibration (score → per-chunk include/exclude) — turns on at this seam.
- **Eval hooks:** emit validation-failure + retry counts as spike telemetry; N2/N3 partial-response
  metric definitions target this shape (flag at Phase 5 — Roadmap).
- **State→eval scoring — ADR 0023 (PM-1, ratified).** The eval runner scores **two signals**: retrieval
  (`expected_sources` in top-k) and grounding (this `state`). Grounding: GROUNDED=pass, **PARTIAL=tracked
  bucket (not a pass)**, REFUSAL/INTEGRITY_FLAGGED=fail (in-corpus), ERROR=excluded+logged; `partial_rate`
  reported alongside `grounded_pass_rate`. The `state→outcome` map is the Grounder↔eval-runner contract,
  in the core so V1 eyeball and V3 harness read one definition.
- **UI:** verified-GROUNDED vs. INTEGRITY_FLAGGED must be visually distinct (N11 extension; reconcile
  as a state-variant of ADR 0012's answer surface, not a sixth screen).
```
