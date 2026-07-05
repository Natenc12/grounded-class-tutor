# 0009. V1 is single-shot Q&A; multi-turn is an additive upgrade

- **Date:** 2026-07-04
- **Status:** accepted

## Context
Real learning is conversational — the value shows up when a student asks follow-ups about details
that surface in an answer. So multi-turn is clearly desirable *eventually*. The question at Phase 2
was whether V1 must carry it, which hinges on **how hard the later upgrade is**. If additive, V1 can
stay single-shot and not pay for conversation state before the grounded core is even proven.

## Decision
**V1 is single-shot (stateless cited Q&A).** Multi-turn is deferred to V-next but the architecture
is shaped so the upgrade is **additive, not a reshape**. Multi-turn adds exactly three things:
1. a **query-condensation** pre-step that rewrites a follow-up into a standalone question using
   history — it slots *in front of* the Retriever and never changes it;
2. a **`conversations`/`messages`** store (a new table, `conversation_id`) — additive to the schema;
3. **history in the Grounder prompt** — additive to the template.

The V1 core pipeline (Retriever → Grounder) is untouched by all three.

**Two V1 obligations that keep the upgrade cheap:**
- The **core is a callable library with a clean query entry point** — don't weld "the question" into
  the HTTP handler as a single baked string (also required for the eval harness / spikes to drive it).
- **No conversation table in V1** — single-shot references no conversation, so there's nothing to
  reshape later (grow-as-you-go).

## Alternatives considered
- **Multi-turn in V1** — rejected: adds condensation logic + conversation state before the grounded
  core is proven, competing with the retrieval-quality proof for attention. The upgrade being
  additive means V1 loses nothing by waiting.
- **Never multi-turn** — rejected: conversational follow-up is where the learning value concentrates;
  this is a first-class V-next item, not a permanent non-goal.

## Consequences
- Extends ADR 0002's "cited Q&A only" — in V1 that means **single-shot**.
- The core's interface must take a query (history-ready), reinforcing the callable-core shape.
- Roadmap (Phase 5) sequences multi-turn as the first V-next interaction upgrade.
