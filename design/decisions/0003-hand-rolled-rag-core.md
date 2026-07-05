# 0003. Hand-rolled RAG core (no orchestration framework)

- **Date:** 2026-07-04
- **Status:** accepted

## Context
The RAG orchestration — parse → chunk → embed → vector search → cited-prompt construction — can
be hand-rolled or delegated to a framework (LlamaIndex / LangChain). Two goals were in apparent
tension: this is a real production app (arguing for framework velocity) and a vehicle for learning
+ demonstrating RAG fundamentals (arguing for hand-rolling). We tested the tension explicitly,
including the portfolio question: does "production experience via a framework" or "demonstrated
understanding of RAG" read better to a recruiter?

## Decision
**Hand-roll the RAG core.** Reach for a framework only if build velocity later demands it, and
record that as a superseding decision if so.

## Alternatives considered
- **LlamaIndex / LangChain** — faster plumbing, gets to retrieval experiments sooner. Rejected:
  (1) the plumbing is the *boring* part; the learning that matters — chunking strategy, top-k
  tuning, retrieval eval, refusal calibration — is not what a framework hides. (2) Frameworks
  fight you exactly where this product is non-standard: threading **source metadata (file / page /
  slide) through chunk → index → citation**, which is the citation-fidelity path the whole product
  rests on. (3) On a portfolio, a framework integration reads as "followed the quickstart," not as
  depth; the *production* signal comes from the eval + observability + RLS layer around the core,
  which we get either way.

## Consequences
- We own the parse/chunk/embed/search/prompt pipeline directly and can instrument it for eval.
- The citation metadata contract is a first-class design concern from ingestion onward.
- An evaluation harness (retrieval precision/recall, answer faithfulness) is expected, since we're
  not inheriting one — and it's part of what makes the project read as production-grade.
- Embeddings *provider* is a separate, still-open choice (Voyage default, confirm via the
  retrieval spike) — see requirements.
