"""Configuration + the embedding-consistency anchor (ADR 0018).

The one rule that must never be violated: index-time and query-time embeddings use the
*identical* model + version, or similarity search is meaningless. We enforce that by making
this module the SINGLE source of the active embedder id. The write path stamps
`chunks.embedding_model_id` from it; the read path (Slice 1's Retriever) asserts equality
against it. Never hardcode the model id anywhere else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

# --- The embedding invariant anchor (ADR 0018) ---------------------------------------------
# Provisional default (ADR 0005 / roadmap Slice 0). `EMBEDDING_DIM` is the one schema-coupled
# spike variable — changing the embedder to a different dim requires a migration + re-index.
ACTIVE_EMBEDDING_MODEL_ID = "text-embedding-3-small"
EMBEDDING_DIM = 1536

# Generation provisional default (ADR 0007); the GPT-vs-Claude bake-off is Spike Pass 2.
DEFAULT_GENERATION_MODEL = "gpt-4o-mini"


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
