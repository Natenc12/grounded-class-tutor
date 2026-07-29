"""Apply every migrations/*.sql in filename order. Idempotent (the SQL uses IF NOT EXISTS),
so re-running is safe. Plain SQL by design — hand-rolled, no Alembic weight in V1 (ADR 0003)."""

from __future__ import annotations

import pathlib

import psycopg

from gct.config import load_settings

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def main() -> None:
    sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        print("no migrations found.")
        return
    with psycopg.connect(load_settings().database_url) as conn:
        for path in sql_files:
            print(f"applying {path.name} ...")
            conn.execute(path.read_text())
        conn.commit()
    print(f"done — {len(sql_files)} migration(s) applied.")


if __name__ == "__main__":
    main()
