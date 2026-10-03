# 0033. Local desktop is the primary product architecture

- **Date:** 2026-10-02
- **Status:** accepted — supersedes hosted runtime decisions 0005, 0006, 0007, 0010,
  0011, 0012, 0018 and 0032; amends 0002, 0004, 0017, 0020 and their implementation
  amendments where they require hosted infrastructure.

## Context

Nathan selected a local app using his authorized ChatGPT plan, then requested that
the whole repository stop being set up for its original hosted stack. The account
proof works, but its Grounder still imported the Postgres retriever's source type,
the default package installed server dependencies, and current docs still called
Postgres/API-first the product architecture. The preview also had no durable library.

## Decision

Make Electron plus a bounded Python local service the primary runtime. Keep the
existing parser, chunker, Grounder, provenance and evaluation contracts. Move neutral
source types out of database-specific modules. Electron owns credentials and model
traffic; Python owns local storage/search and grounding. No HTTP server or paid
API/embedding fallback is required by the local product.

Use SQLite with original bounded document bytes, metadata, parsed pages, chunks
and rebuildable FTS5 search. Publish complete imports atomically. Scope reads by
class before ranking and preserve document/chunk identity independently of filenames.
Use keyword retrieval as a measured baseline, not a claim of semantic equivalence.
Its rank score is not normalized cosine similarity or calibrated support confidence.
The existing Grounder still owns response states; structural citations are not
factual-entailment verification.

The local OS user owns the library. A ChatGPT account authorizes inference, not
library ownership. Retain the existing encrypted credential/app-data path until a
separate explicit migration exists. Signing out never deletes course material.

Isolate the old hosted stack behind an explicit transitional dependency/test scope,
then retire unused adapters, migrations, jobs, web client and paid smoke setup after
the local replacement passes. Preserve the old local database and ignored corpus.
Git revision `0b004e7` preserves the hosted code and operational documentation.

Make local CI authoritative only after its clean dependency install and integration
gates pass. Maintain protected merges throughout the cutover. A coordinated fleet
with independent reviews and a single integrator delivers this migration in the
original repository. Packaging is a separate acceptance milestone; a checkout
launch does not establish a downloadable release.

## What remains valid

Hand-rolled reusable core (0003), provider separation, single-shot library boundary
(0009/0013), coverage/citation/error behavior (0014–0016), never-span chunk provenance
(0019), and separate retrieval/grounding evaluation (0021/0023) remain. Atomic
publication and safe bounded input principles remain; Postgres-specific transaction,
lease, worker and vector-model mechanics are historical implementation choices.
The previous spike measurements (0026) apply only to their recorded configuration.

## Alternatives considered

- Keep desktop as a permanent sidecar: leaves contradictory defaults and two products.
- Rewrite parsing and grounding in JavaScript: discards tested logic without solving
  local persistence or source fidelity.
- Ship Postgres and the HTTP worker stack inside desktop: carries server operations
  into a single-user app without a current requirement.
- Add remote or local embeddings immediately: remote spending is unauthorized;
  local model downloads/runtime complexity should follow measured lexical failures.
- Store originals and database metadata separately: requires a cross-filesystem
  publication/recovery protocol. Bounded originals in SQLite make first-release
  durability and backup transactional.

## Consequences

The architecture, data model, roadmap, setup instructions and agent guidance now
name the local product. This accepted design is not a completion claim: durable
library, desktop integration and installer readiness each need their own evidence.
The existing subscription connection and original documents must survive migration.

The [official sign-in limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
remain binding at the account adapter. Unsupported hosted tools are not a substitute
for the local library. Never infer universal account eligibility from one live proof.
