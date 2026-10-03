# Start here

Grounded Class Tutor's primary architecture is a local desktop app.

1. [ADR 0033](decisions/0033-local-desktop-primary.md) records the local pivot and
   the hosted decisions it replaces.
2. [Architecture](architecture.md) defines component ownership and trust boundaries.
3. [Data model](data-model.md) defines local durability and source identity.
4. [Roadmap](roadmap.md) gives migration and release acceptance milestones.
5. [Handoff](HANDOFF.md) explains how the fleet implements and reviews work.
6. [Local proof](local-app-proof.md) records what was actually demonstrated.
7. [Quality evidence](local-quality-validation.md) separates retrieval measurements
   from independently reviewed connected answers and records known limitations.
8. [Personal Mac packaging](../desktop/PACKAGING.md) describes the self-contained
   preview, isolated runtime checks and remaining public-release requirements.
   [Observed packaging evidence](local-packaging-validation.md) records exactly
   which checks passed and where native verification is pending.

The older component specifications, diagrams, hosted launch plan, maturity ladder,
and Slice 4 issue descriptions remain historical context where they conflict with
ADR 0033. They must not silently steer new work back to Postgres, Supabase, an HTTP
API, paid embeddings, or a separate web product. The Git history at `0b004e7`
preserves the pre-pivot runtime and setup instructions.

Do not equate approved architecture with implementation or a passing fixture with
measured factual accuracy. Read the current code, issue state and test evidence.
