# Local desktop roadmap

The GitHub issue board owns assignments and completion. This document defines
acceptance milestones under ADR 0033; it is not a second checklist of issue status.

1. **Portable core and local defaults.** A fresh default install runs the real
   parser, chunker, Grounder and desktop bridge without Postgres, pgvector, OpenAI
   API client, dotenv, FastAPI or uvicorn. Current instructions describe one product.
2. **Durable local library.** Classes and original PDF/PPTX bytes survive process
   exit. Original+pages+chunks publish atomically; interrupted imports, duplicates,
   deletion, corrupt/newer databases and consistent backup have failure tests.
3. **Class-scoped local retrieval.** Automatic evidence selection runs locally.
   Measure expected-page recall separately from grounding quality; test duplicate
   filenames, multiple classes, paraphrases, tables and unsupported questions.
4. **Desktop integration.** Create/select classes, import/reopen files, ask with
   bounded evidence, inspect cited source and cancel safely. Required functional
   controls precede the deferred visual redesign. Test a fresh process/restart and
   one authorized synthetic live question after mocked integration passes.
5. **Hosted retirement.** Remove unused HTTP server, distributed worker, Postgres
   schema, paid provider adapters, web client and smoke paths after replacement
   passes. Keep the existing database/corpus untouched and a historical Git revision.
   Cut branch protection over to successful local gates without an unprotected gap.
6. **Downloadable release.** Bundle Python/core resources and Electron. Test install,
   launch and upgrade without developer tools or checkout paths; preserve credentials
   and library. Signing/distribution/updates need their own concrete release setup.

The working account proof is evidence for one account and a narrow sample, not
completion of this roadmap. Hosted planning and old slice epics are historical.
No cloud deployment, paid API setup, OCR, multi-user tenancy or autonomous model
background work is required for this local refactor.
