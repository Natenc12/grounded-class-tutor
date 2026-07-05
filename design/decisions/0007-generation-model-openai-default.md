# 0007. Generation model default: OpenAI (consolidation, swappable, spike-confirmed)

- **Date:** 2026-07-04
- **Status:** accepted (default; final production model confirmed by the spike)

## Context
Embeddings default to OpenAI (ADR 0005) and generation sits behind a provider interface (ADR 0004),
so we need a *default* generation model to build V1 on. Nate proposed OpenAI for both — "use OpenAI
for the whole operation" — for operational simplicity.

## Decision
Default the generation model to **OpenAI (a GPT tier)** for V1, kept behind the generation provider
interface (ADR 0004). **Confirm the final production model empirically in the spike** — does it
follow citation format and refuse faithfully on the real corpus? — the same pattern as embeddings
(ADR 0005). **Claude remains a first-class swap candidate.**

## Rationale
- The benefit is **operational, not technical**: one API key / billing / SDK and less setup friction
  for a solo build. Real, but small.
- **Nuance recorded:** embeddings and generation do **not** need to share a vendor — they're
  independent HTTP calls and retrieved chunk text is vendor-neutral. "Cleaner to match" is
  convenience, not a requirement; the coupling is optional.
- The default is **low-stakes**: generation is swappable by design (ADR 0004) and both GPT and Claude
  are top-tier at the grounding task (citation-following + faithful refusal). So operational
  simplicity breaks the tie now, and the spike settles quality.

## Alternatives considered
- **Claude default (the prior choice)** — equally strong for the task; no a-priori quality reason to
  prefer either. Demoted to swap candidate; consolidation tips the *default* to OpenAI.
- **Hardcode one vendor, no interface** — already rejected (ADR 0004); a single-vendor *default* does
  **not** remove the abstraction.

## Consequences
- Updates the generation default in **ADR 0006** and requirement **F14**.
- The spike's model bake-off now covers **both** embeddings (ADR 0005) **and** generation.
- Swappability is intact — this is only which model we *start* on, not a re-coupling.
