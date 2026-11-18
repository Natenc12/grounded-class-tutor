"""The free gate, as one command: lint, format, migrations, and every test that costs nothing.

Run it as `./verify`. It never touches the database DATABASE_URL names and cannot spend money.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from psycopg import sql

REPO = Path(__file__).resolve().parent.parent
SCRATCH_DB = "gct_verify"


class VerifyError(Exception):
    pass


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres") or not parts.netloc:
        raise VerifyError(
            "DATABASE_URL must be a postgresql:// URL so verify can derive its scratch database."
        )
    return urlunsplit(parts._replace(path=f"/{name}"))


def source_url(environ: dict[str, str], dotenv_path: Path) -> str:
    url = environ.get("DATABASE_URL") or dotenv_values(dotenv_path).get("DATABASE_URL")
    if not url:
        raise VerifyError(
            "No DATABASE_URL in the environment or .env. Verify needs a Postgres server to "
            f"create its scratch database '{SCRATCH_DB}' on."
        )
    return url


def child_env(environ: dict[str, str], scratch: str) -> dict[str, str]:
    return {**environ, "DATABASE_URL": scratch, "OPENAI_API_KEY": ""}


def recreate_scratch(server_url: str) -> None:
    name = sql.Identifier(SCRATCH_DB)
    try:
        with psycopg.connect(with_database(server_url, "postgres"), autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(name))
            conn.execute(sql.SQL("create database {}").format(name))
    except psycopg.OperationalError as exc:
        raise VerifyError(f"Could not reach Postgres to create '{SCRATCH_DB}': {exc}") from exc


def steps() -> list[tuple[str, list[str]]]:
    py = sys.executable
    migrate = [py, "scripts/migrate.py"]
    return [
        ("lint", [py, "-m", "ruff", "check"]),
        ("format", [py, "-m", "ruff", "format", "--check", "."]),
        ("migrations", migrate),
        ("migrations again (idempotent)", migrate),
        ("tests (free only)", [py, "-m", "pytest", "-m", "not live", "-q"]),
    ]


def main() -> int:
    try:
        server = source_url(dict(os.environ), REPO / ".env")
        env = child_env(dict(os.environ), with_database(server, SCRATCH_DB))
        recreate_scratch(server)
    except VerifyError as exc:
        print(f"VERIFY FAILED before any check ran: {exc}")
        return 1

    for name, command in steps():
        print(f"\n== {name}", flush=True)
        if subprocess.run(command, cwd=REPO, env=env).returncode != 0:
            print(f"\nVERIFY FAILED at: {name}")
            return 1

    print(
        f"\nVERIFY PASSED ({len(steps())} checks, scratch database '{SCRATCH_DB}', no paid calls)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
