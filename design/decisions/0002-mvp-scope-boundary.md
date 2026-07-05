# 0002. MVP scope boundary

- **Date:** 2026-07-04
- **Status:** accepted — **tenancy clause amended 2026-07-04 by ADR 0004** (RLS moved from
  day-one to V3; `owner_id` schema hedge). Interaction/inputs/frontend scope unchanged.

## Context
The whole product rests on retrieval quality + faithful grounding being trustworthy. The MVP must
prove exactly that and nothing that dilutes it. Several scope axes were parked at Discovery for a
kickoff decision.

## Decision
The v1 spine is the **smallest slice that proves trustworthy grounded retrieval**:
- **Interaction:** **cited Q&A only** — ask a question → retrieved, cited answer → refuse when the
  answer isn't in the corpus. No Socratic tutoring in v1.
- **Inputs:** **PDFs and slides only**, uploaded. No photo capture / OCR in v1.
- **Frontend:** **React PWA**, mobile-first. No native app in v1 (no push need forces it).
- **Tenancy:** **one class per user.** *(Superseded by ADR 0004:* dogfooding is solo, so v1 uses a
  **single hardcoded user with no auth**, but the schema carries an `owner_id` column from day one;
  **real per-user auth + Supabase RLS land in V3**, as "turn on enforcement," not a schema reshape.
  Multi-*class-per-user* remains deferred.*)*

**Deferred to roadmap (explicitly not v1):** Socratic tutoring mode; photo/OCR of handwritten
notes; auto-generated quizzes; multi-class-per-user; shared/class-wide libraries; retrieval
re-ranking.

## Alternatives considered
- **+ Socratic mode in v1** — rejected: larger prompt-engineering surface that competes with the
  retrieval-quality proof for attention. It's a roadmap item.
- **+ Photo/OCR from day one** — rejected: OCR is a whole subsystem and would become a *second*
  empirical make-or-break competing with retrieval. Defer.
- **Single user / single class, no RLS yet** — rejected: the team needs isolated corpora on day
  one, and retrofitting a security boundary is exactly the kind of thing you don't defer.
- **Native app (Expo RN) in v1** — rejected: v1 has no push requirement; PWA is lighter and the
  upload flow works fine in a browser. Native is reconsidered if/when photo capture lands.

## Consequences
- Requirements are scoped to this spine; roadmap sequences the deferrals.
- RLS isolation and per-user auth are load-bearing v1 requirements with their own success criteria.
- Citations (source metadata → tap-to-source) are in scope for v1 — they're part of "trustworthy."
