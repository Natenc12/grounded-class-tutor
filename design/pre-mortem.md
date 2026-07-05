# Pre-Mortem — Grounded Class Tutor

**Phase:** 6 — Handoff (adversarial pass, runs *before* the readiness gate)  **Rigor:** Deep (ADR 0001)

> **Frame:** Assume the V1 build shipped and failed — or stalled because a builder hit a wall and was
> forced to make a design call *we* should have made. Work backward to why. Each finding is one line,
> with a disposition: **FIX** (resolve now, usually a micro-ADR or one-line spec edit) or **CARRY**
> (accept into V1 as a stated Open Assumption). Scope: the V1 walking skeleton + the two-pass spikes.
> This is a hunt for load-bearing gaps, not a re-review of what's already sound.

---

## Findings

### PM-1 — The eval file can't *score*: 5 Grounder states vs. a 2-value `expectation` — **FIX (ADR)**
The Grounder emits `GROUNDED | PARTIAL | REFUSAL | INTEGRITY_FLAGGED | ERROR` (grounder spec §8), but
`eval/questions.jsonl.expectation` is binary `answer | refuse` (ADR 0021 §3). **Nowhere is the collapse
defined** — is `PARTIAL` a pass for an `answer` question? Does `INTEGRITY_FLAGGED` count as answered? Is
`ERROR` excluded from the denominator? The eval artifact's whole job is to make spike outputs *rankable*
(ADR 0021 §obj); ranking Pass-1 chunking-vs-generation results forces exactly this call. A builder can't
"eyeball the smoke suite" (§5) consistently without it, and it's a **product-definition** decision, not a
builder's to improvise. *Sharpest finding — the differentiator's only V1 test can't conclude without it.*

### PM-2 — `Embeddings.embed(texts)` on a real deck can exceed the provider batch/token cap — **FIX (spec edit)**
Worker step 4 embeds **all** chunks in one `Embeddings.embed(texts)` call (worker spec §4); OpenAI caps a
request at 2048 inputs / ~300k tokens. A real 50-page reading (C5 = real course materials, N6 = ~50-page
deck) can blow that on the *first* big ingest — surfacing as a hard failure, mis-labeled `transient_exhausted`,
so the file simply never becomes `ready`. Currently filed as "batching = tuning, not contract" (worker
open items) — but a request that exceeds the cap is a **correctness floor**, not throughput. *Fix: the
provider adapter owns sub-batching under the API limit, below the `embed()` interface — the worker's
"embed all" stays honest and the contract is unchanged.*

### PM-3 — "In-process poll worker" leaves the process topology (and event-loop blocking) unmade — **FIX (ADR clarify)**
ADR 0011 says "in-process poll worker" but never says *in which process*, and embed/parse are slow blocking
calls. If the poll loop runs as an asyncio task inside the FastAPI/uvicorn process, a synchronous embed call
**starves request handling** (status polls, new asks hang). A builder must choose: separate `worker.py` vs.
background thread vs. asyncio+threadpool — a real liveness call ADR 0011 leaves open. *Fix: pin the V1
topology (recommend a separate worker process against the same Postgres — simplest, can't block the API,
still one datastore) as a one-line addendum to ADR 0011.*

### PM-4 — The Slice-1 inline pipeline ↔ Slice-2 async worker seam isn't drawn — **FIX (one-line note)**
Roadmap Slice 2 says "wrap the **proven inline pipeline** in the async worker" — but the worker spec is one
integrated flow that *starts* with "Claim the job; set status=processing" (§1). The factoring it assumes —
a **pure pipeline** (parse→chunk→embed→prepare→index-tx) separable from the **job shell** (claim/lease/
status/retry) — is never named. A builder who bakes status/claim into the pipeline pays a Slice-2 refactor.
Sub-question: does Slice-1's inline path create a `files` row and run the `UPDATE files SET status='ready'`
in the index tx, or skip status until Slice 2? *Fix: name the pipeline/shell seam in the roadmap (Slice 1
builds the pure pipeline + a minimal `files` row that goes straight to `ready`; Slice 2 wraps it).*

### PM-5 — No "re-index the whole corpus" operation, which Spike Pass 2 (embedder swap) requires — **CARRY**
Spike Pass 2 bakes off 3-small vs. voyage-3 and says "re-index if voyage wins (C4)." Re-indexing = re-run
ingestion for **every file** under the new embedder — but no corpus-wide re-enqueue is specced, and the
worker only re-indexes *per file* on re-delivery. Worse, if the default embedder changes without a full
re-index, a class holds mixed `embedding_model_id` and the Retriever's consistency guard **ERRORs the whole
class** (retriever §Failure). *Carry as an Open Assumption: V1 re-index is an ops script that re-enqueues
all files; changing the embedder without running it is a known foot-gun the single dogfooder avoids. Name
it so Pass 2 doesn't discover it.*

---

## The meta-risk (already owned — restated, not new)
**The differentiator is empirical and unproven at the V1 gate.** Grounding/refusal quality (N1–N4) ships
crude and unmeasured (ADR 0004); a green gate means *buildable*, not *validated*. This is **already owned** —
it's the gate caveat, and Spike Pass 1 (validate-before-thicken, ADR 0022) is the de-risker. The pre-mortem
adds nothing here except to insist the caveat rides visibly on the gate. PM-1 is what makes Pass 1 able to
actually conclude.

---

## Disposition rollup
| # | Finding | Disposition | Becomes |
|---|---|---|---|
| PM-1 | Eval state→expectation scoring rule undefined | **FIX (ratified)** | ADR 0023 — two-signal scoring (retrieval ∥ grounding; PARTIAL tracked, not a pass) |
| PM-2 | Embed batch exceeds provider cap = correctness floor | **FIX** | spec edit (adapter owns sub-batching) + note in ADR 0013/worker |
| PM-3 | Worker process topology / event-loop blocking | **FIX** | one-line addendum to ADR 0011 |
| PM-4 | Inline↔async shared-core seam not named | **FIX** | one-line roadmap note (Slice 1/2) |
| PM-5 | No corpus-wide re-index for the embedder swap | **CARRY** | Open Assumption (V1 = re-enqueue script) |

**Gate consequence:** PM-1 is load-bearing (the differentiator's V1 test can't score without it) — it should
land as an ADR **before** the gate grades green. PM-2/3/4 are cheap, real fixes. PM-5 is an honest carry.
None reopens a closed contract; all are edits *within* the existing shape.
