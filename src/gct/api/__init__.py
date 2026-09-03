"""The HTTP adapter (Slice 3) - a thin FastAPI over the callable core, and nothing more.

The seam is library-callable vs. adapter (ADR 0009, `design/architecture.md`): a handler
validates, calls ONE library callable, and renders. No grounding, scoping, or status decision is
made here - each of those already has an owner below this package, and a copy in a handler would
be a second writer for it.

What this package owns is the COMPOSITION ROOT (issue #104): the per-request connection that a
library writer will accept (`deps.get_conn`), the one source of the V1 owner (`deps.owner_id`),
the app-wide provider singletons `ask()` takes, the startup requirements, and the error envelope
every failure wears (`errors`). The route modules under `routers/` each belong to their own issue
and are mounted here so none of them edits `app.py`. Spec: `design/components/api.md`.
"""
