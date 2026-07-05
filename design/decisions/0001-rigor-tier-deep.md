# 0001. Rigor tier: Deep

- **Date:** 2026-07-04
- **Status:** accepted

## Context
At the end of Discovery we must set a rigor tier (Sketch / Standard / Deep) that governs ceremony
and document count for the rest of design. Grounded Class Tutor is a real production app intended
for a team of 3 to ship, and its make-or-break — retrieval quality + faithful grounding — is
empirically risky and can't be settled on paper. The proposal leaned "Standard leaning Deep."

## Decision
Run the project at **Deep** rigor: full phases with separate artifacts, per-component specs,
thorough ADRs, adversarial depth concentrated on the retrieval/grounding core, and a pre-mortem
before the readiness gate.

## Alternatives considered
- **Standard** — lighter ceremony, fewer docs. Rejected: the retrieval core is high-uncertainty
  and high-stakes; it deserves the adversarial machinery (component triage, pre-mortem ledger)
  that Deep brings. Under-investing there is the most likely way this project quietly fails.
- **Sketch** — not viable for a multi-component production app.

## Consequences
- Component Design (Phase 3) will triage by criticality: the retrieval + grounding components earn
  adversarial depth; CRUD/upload plumbing earns short specs.
- Deep does **not** mean gold-plating trivial parts — right-size within the project too.
