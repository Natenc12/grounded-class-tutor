# 0012. V1 client — a minimal React SPA, scoped and off the critical path

- **Date:** 2026-07-04
- **Status:** accepted

## Context
Open decision ③ framed the V1 client as *real minimal React PWA* vs. *an even-thinner harness*
(CLI / notebook / Streamlit). The pull toward a real UI is that F3, N10, and N11 are all **V1 P0**
and UI-shaped. The pull toward a harness is that the client is not the product's risk surface — the
make-or-break is empirical retrieval + faithful grounding (the V3 gate), de-risked by spikes against
the core, not by any UI.

## Decision
**V1 ships a real but minimal React SPA — not a PWA, and not a throwaway harness.**

- **The two-way choice collapses.** The architecture already made the eval harness and spike scripts
  *peer callers of the core's programmatic entry point*. So the job a thinner harness would do —
  exercise the retrieve→ground→cite loop headlessly — **is already covered.** Nothing is left for an
  interim throwaway UI to earn.
- **N11 needs a real UI.** "Citations render cleanly and legibly inline — the trust surface" is a
  *rendering* requirement that curl / a notebook / Streamlit cannot validate. Same for N10's
  status-polling "ready to ask" signal. This is the one place the client carries
  product-differentiating weight, not just plumbing — so it must be real.
- **SPA, not PWA.** The *progressive* part (offline, installable, service workers) buys nothing in
  V1. Spec a plain minimal React SPA; don't carry offline/service-worker complexity we don't need.

### Guardrails (these are where this goes wrong)
1. **Scoped to exactly the five V1 P0 surfaces:** upload + ingestion status (F1/F3), ask box (F6),
   answer-with-inline-citations (F7/N11), refusal state (F8). **Explicitly out of V1:** no auth/login
   UI (F12 = single hardcoded user), no file/class delete (F4 = V2), no tap-citation-to-source
   (F9 = V2). A single-purpose shell, not an app.
2. **Off the critical path.** The SPA *follows* the core, it does not lead. The core loop
   + spikes de-risk the differentiator first; the SPA is the thin shell that proves N10/N11 once the
   core produces citations worth rendering.

## Alternatives considered
- **Thinner harness (CLI / notebook / Streamlit) for V1** — rejected: redundant with the core's
  programmatic entry point (already a peer-caller seam), and it cannot validate N11/N10, which are
  V1 P0. It would be a throwaway that covers nothing the harness doesn't already cover.
- **Full React PWA** — rejected for V1: offline/installability is not a V1 need; the *progressive*
  surface is complexity without payoff. Revisit only if an offline/mobile-install use case becomes real.
- **Full-featured app now** — rejected: gold-plating. Delete, citation-jump, and auth are V2/V3;
  pulling them into the V1 client inflates the non-risk surface.

## Consequences
- The `architecture.md` caller box reads **"React SPA (V1)"**, not PWA.
- Phase 3 client spec is bounded to the five P0 surfaces above; V2/V3 surfaces are named as *out*.
- Reinforces the sequencing guard in `state.md` — build the differentiator (core + spikes) first;
  the client is the shell that proves the human-facing P0s, not the thing that carries the risk.
