"""Postgres access. A thin helper now; the data layer stays local Postgres in V1 and swaps to
Supabase in V2 with no rewrite (C4 / ADR 0006)."""

from __future__ import annotations

import psycopg
from pgvector.psycopg import register_vector

from .config import load_settings


def connect() -> psycopg.Connection:
    """Open a connection with the pgvector type adapter registered.

    Requires the `vector` extension to already exist (created by migrations/0001_init.sql),
    so run scripts/migrate.py first. The migration script itself connects WITHOUT this helper.
    """
    conn = psycopg.connect(load_settings().database_url)
    register_vector(conn)
    return conn
