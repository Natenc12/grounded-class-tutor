"""Slice 0 exit test. Proves the foundation is wired — no app logic yet:
  1. the 4 tables exist and pgvector round-trips a vector of the right dim, and
  2. both provider interfaces round-trip (embedding has the right dim + model_id; generation
     returns text).
Run after scripts/migrate.py, with DATABASE_URL and OPENAI_API_KEY set in .env."""

from __future__ import annotations

from gct.config import ACTIVE_EMBEDDING_MODEL_ID, EMBEDDING_DIM
from gct.db import connect
from gct.providers.openai_provider import OpenAIEmbeddings, OpenAIGeneration


def check_db() -> None:
    with connect() as conn:
        rows = conn.execute(
            "select table_name from information_schema.tables where table_schema = 'public'"
        ).fetchall()
        present = {r[0] for r in rows}
        expected = {"classes", "files", "chunks", "jobs"}
        missing = expected - present
        assert not missing, f"missing tables {missing} — run scripts/migrate.py"
        # prove pgvector actually round-trips a vector of the configured dim
        vec = [0.1] * EMBEDDING_DIM
        got = conn.execute("select %s::vector", (vec,)).fetchone()[0]
        assert len(got) == EMBEDDING_DIM
    print(f"  db ok — tables {sorted(expected)} present; pgvector round-trips dim={EMBEDDING_DIM}")


def check_embeddings() -> None:
    emb = OpenAIEmbeddings()
    vectors = emb.embed(["the mitochondria is the powerhouse of the cell"])
    assert len(vectors) == 1
    assert len(vectors[0]) == emb.dim == EMBEDDING_DIM
    assert emb.model_id == ACTIVE_EMBEDDING_MODEL_ID
    print(f"  embeddings ok — model={emb.model_id} dim={emb.dim}")


def check_generation() -> None:
    gen = OpenAIGeneration()
    text = gen.generate(
        [
            {"role": "system", "content": "Reply with exactly one word."},
            {"role": "user", "content": "Say 'pong'."},
        ]
    )
    assert text.strip(), "generation returned empty text"
    print(f"  generation ok — model={gen.model_id} reply={text.strip()!r}")


def main() -> None:
    print("Slice 0 smoke:")
    check_db()
    check_embeddings()
    check_generation()
    print("PASS — foundation is wired.")


if __name__ == "__main__":
    main()
