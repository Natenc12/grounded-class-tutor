"""Run the real app end to end for free and print what happened.

Starts the API and the worker on a scratch database with stand-in models, then drives
create class, upload, wait, ask over HTTP. Arguments are passed to scripts/http_smoke.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
from dotenv import dotenv_values
from psycopg import sql

REPO = Path(__file__).resolve().parents[1]
SCRATCH_DB = "gct_drive"


class DriveError(Exception):
    pass


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgres") or not parts.netloc:
        raise DriveError(
            "DATABASE_URL must be a postgresql:// URL so the scratch database can be derived."
        )
    return urlunsplit(parts._replace(path=f"/{name}"))


def source_url(environ: dict[str, str], dotenv_path: Path) -> str:
    url = environ.get("DATABASE_URL") or dotenv_values(dotenv_path).get("DATABASE_URL")
    if not url:
        raise DriveError(
            "No DATABASE_URL in the environment or .env. A Postgres server is needed to create "
            f"the scratch database '{SCRATCH_DB}' on."
        )
    return url


def child_env(environ: dict[str, str], scratch: str, staging: str) -> dict[str, str]:
    return {
        **environ,
        "DATABASE_URL": scratch,
        "OPENAI_API_KEY": "",
        "GCT_PROVIDERS": "standin",
        "GCT_STAGING_DIR": staging,
    }


def recreate_scratch(server_url: str) -> None:
    name = sql.Identifier(SCRATCH_DB)
    try:
        with psycopg.connect(with_database(server_url, "postgres"), autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(name))
            conn.execute(sql.SQL("create database {}").format(name))
    except psycopg.OperationalError as exc:
        raise DriveError(f"Could not reach Postgres to create '{SCRATCH_DB}': {exc}") from exc


def main(argv: list[str]) -> int:
    with tempfile.TemporaryDirectory(prefix="gct-drive-") as staging:
        try:
            server = source_url(dict(os.environ), REPO / ".env")
            env = child_env(dict(os.environ), with_database(server, SCRATCH_DB), staging)
            recreate_scratch(server)
        except DriveError as exc:
            print(f"DRIVE FAILED before the app started: {exc}")
            return 1

        print(
            f"FREE RUN: stand-in models, scratch database '{SCRATCH_DB}', no API key.\n"
            "This proves the plumbing. The answers are word matching, not a model reading.\n",
            flush=True,
        )
        for command in (["scripts/migrate.py"], ["scripts/http_smoke.py", *argv]):
            done = subprocess.run([sys.executable, *command], cwd=REPO, env=env)
            if done.returncode != 0:
                print(f"\nDRIVE FAILED at: {command[0]}")
                return done.returncode
    print("\nDRIVE PASSED (free run, stand-in models)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
