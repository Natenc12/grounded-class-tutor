"""Configuration + the embedding-consistency anchor (ADR 0018).

The invariant: index-time and query-time embeddings use the *identical* model + version, or
similarity search is meaningless. Two distinct rules enforce it — don't collapse them:

  - *Which embedder gets constructed* is sourced only from `ACTIVE_EMBEDDING_MODEL_ID`, here.
    Never hardcode the model id anywhere else.
  - *What gets recorded about a run* is NOT sourced from here: `chunks.embedding_model_id` is
    stamped from `embedder.model_id` — the model that ACTUALLY produced the stored vectors —
    and the Retriever's guard compares that stamp against the active embedder's `model_id`.
    Sourcing both sides from config would make the guard compare config to itself and never
    fire (ADR 0018; the mismatch test in tests/gct/retriever/ proves the guard is real).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- The embedding invariant anchor (ADR 0018) ---------------------------------------------
# Provisional default (ADR 0005 / roadmap Slice 0). `EMBEDDING_DIM` is the one schema-coupled
# spike variable — changing the embedder to a different dim requires a migration + re-index.
ACTIVE_EMBEDDING_MODEL_ID = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Generation provisional default (ADR 0007); the GPT-vs-Claude bake-off is Spike Pass 2.
DEFAULT_GENERATION_MODEL = "gpt-4o-mini"

# --- The ingest input ceiling (ADR 0029) ----------------------------------------------------
# The largest input the ingest pipeline will embed, counted in whitespace-split words. Past it,
# ingest refuses TERMINALLY (`ParseError("too_long")` -> `files.failed_reason='too_long'`, ADR
# 0020, terminal set extended per ADR 0029) instead of buying an unbounded embedding run.
#
# Why words rather than bytes, pages, or chunks - and why this number - is ADR 0029's argument,
# measured on the dogfood corpus; do not re-derive it here. One knob: `compose`/`ingest_file`
# take `max_words` defaulted to this, so a caller may lower it without a second constant
# existing anywhere.
MAX_INGEST_WORDS = 250_000

# --- File staging (ADR 0010) ----------------------------------------------------------------
# Where an upload's bytes land before the worker opens them (`gct.staging.stage`), and the most
# bytes ONE upload may put there. Both are one-knob defaults in the shape of `MAX_INGEST_WORDS`:
# `stage` takes `max_bytes` / `staging_dir` defaulted to these, so a caller may lower or redirect
# without a second constant existing anywhere. The API skeleton (#104) and the files router
# (#110) MUST read them from here - this module is the only settings layer, and a second one (a
# pydantic Settings, an env read inside a router) would be two writers for one fact.
#
# `MAX_STAGE_BYTES` is PROVISIONAL: the NUMBER is a placeholder no ADR owns yet, and a later ADR
# sets it. ADR 0029's ceiling is counted in words over `parse_file`'s output, so it cannot
# protect the disk - the bytes are already there when it fires. What the placeholder must admit
# is N6's shape, a <=50-page deck with images: the dogfood corpus's fattest deck is 7.11 MB for
# 8 slides (ADR 0029 §1), so ~45 MB at 50, and 100 MiB is that with ~2x headroom. Exceeding it
# is refused at REQUEST time as `StagingError("too_large")` - never a `files.failed_reason`,
# because no `files` row exists yet to carry one (see `gct.staging`).
MAX_STAGE_BYTES = 100 * 1024 * 1024

# Only the WRITER needs this: the worker never looks it up, it opens the absolute path
# `enqueue` stored as `files.staging_ref`. Env-overridable (`GCT_STAGING_DIR`) so a deployment
# can point uploads at a different disk without a code change; the default sits beside the
# dogfood corpus and is gitignored like it (`data/staging/`). Resolved at import so a relative
# value is pinned to the cwd of the process that read it - `staging_ref` must stay openable
# after any later `chdir`.
STAGING_DIR = Path(os.environ.get("GCT_STAGING_DIR", "data/staging")).resolve()

# --- The V1 owner (ADR 0004) ----------------------------------------------------------------
# V1 is ONE hardcoded user with no auth (ADR 0004; ADR 0002's tenancy clause as amended by it).
# Every row the API writes and every scoped query it runs carries this owner_id, so V3 turns
# enforcement (auth + RLS) on over the same column instead of reshaping the schema.
#
# ONE source, on purpose: `gct.api.deps.owner_id` reads it and `scripts/ask_smoke.py` defaults
# `--owner` to it, so the API answers over the SAME corpus the smoke ingested. A second literal
# anywhere would be a second writer of this value, and the day they differ the API silently
# answers from an empty class. Not an environment variable: nothing in V1 has a second user to
# select, and a knob with one legal value is a place for the two copies to start (issue #104).
V1_OWNER_ID = "nate-dogfood"


@dataclass(frozen=True)
class Settings:
    database_url: str
    openai_api_key: str


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql://localhost:5432/grounded_class_tutor"
        ),
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
    )
