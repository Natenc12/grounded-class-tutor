#!/usr/bin/env bash
# Bring a cloud container up to the point where GCT can actually run.
#
# The laptop instructions in README.md are Homebrew-based and don't apply here: cloud
# containers ship Postgres 16 binaries that are installed but not started, and no cluster.
#
# Not a hook. It takes minutes on a cold container, well past a hook's timeout, and most
# sessions never touch the database. Run it when you need the DB:
#
#   bash scripts/cloud-bootstrap.sh
#
# It refuses anywhere but a Linux container running as root, because it rewrites .env.
# GCT_FORCE_BOOTSTRAP=1 overrides that.
#
# Idempotent, and near-instant once the container has been through it.

set -uo pipefail
# Throwaway Linux containers only, and it says so with a non-zero status.
#
# Keyed on the platform, not on CLAUDE_CODE_REMOTE: that variable is set by the Claude
# Code cloud agent, so a plain shell, a CI runner or a systemd unit inside the very same
# container was refused with a message reading "not for a laptop", which is wrong there.
# The hazard this guards is a developer machine - on a Mac the apt and pg_createcluster
# steps fail, a running Homebrew Postgres satisfies the readiness check, and the .env
# rewrite below would overwrite a real DATABASE_URL.
#
# Exit 2, not 0. Exiting 0 on a refusal told `cloud-bootstrap.sh && alembic upgrade` that
# provisioning had succeeded, and CI saw a clean pass with nothing done.
if [ "${GCT_FORCE_BOOTSTRAP:-}" != "1" ]; then
  if [ "$(uname -s 2>/dev/null)" != "Linux" ] || [ "$(id -u 2>/dev/null)" != "0" ]; then
    printf '[gct] %s\n' "this provisions a throwaway Linux container as root and rewrites .env." >&2
    printf '[gct] %s\n' "Refusing here. See README.md for local setup." >&2
    printf '[gct] %s\n' "Override with GCT_FORCE_BOOTSTRAP=1 if you are certain." >&2
    exit 2
  fi
fi

# Anchor to the repo root. Several steps below use relative paths; run from anywhere else
# they write .env into the wrong directory and fail four lines later at migrate.
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
MARKER="$HOME/.gct-cloud-bootstrapped"
DB=grounded_class_tutor
log() { printf '[gct] %s\n' "$*"; }

# PATH first. These containers ship the Postgres binaries off the default PATH, so
# checking the marker before this meant pg_isready always failed and every run redid
# apt-get, uv sync, and migrations.
command -v pg_isready >/dev/null 2>&1 || export PATH="/usr/lib/postgresql/16/bin:$PATH"

if [ -f "$MARKER" ] && pg_isready -q 2>/dev/null; then
  log "already bootstrapped"; exit 0
fi

# --------------------------------------------------------------- postgres + pgvector
apt-get install -y -qq postgresql-16-pgvector 2>/dev/null || log "WARN pgvector install failed"

# Containers ship the binaries with no cluster. Create one if it isn't there, then start it.
pg_lsclusters 2>/dev/null | grep -q '^16 ' || pg_createcluster 16 main 2>/dev/null
pg_ctlcluster 16 main start 2>/dev/null
pg_isready -q || { log "ERROR postgres did not start"; exit 1; }

# The container runs as root; postgres refuses a root superuser login, so work through
# the postgres unix user and hand ownership to whoever we actually are.
ME=$(id -un)
su postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$ME'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "createuser -s '$ME'" 2>/dev/null
su postgres -c "psql -tAc \"SELECT 1 FROM pg_database WHERE datname='$DB'\"" 2>/dev/null | grep -q 1 \
  || su postgres -c "createdb -O '$ME' $DB" 2>/dev/null
su postgres -c "psql -d $DB -c 'CREATE EXTENSION IF NOT EXISTS vector'" >/dev/null 2>&1 \
  || { log "ERROR pgvector extension unavailable; ingest and retrieval cannot work"; exit 1; }

# ------------------------------------------------------------------------- app config
# DATABASE_URL is read from .env, which is gitignored and so absent in a fresh clone.
# No host in the URL, on purpose. Naming localhost makes libpq use TCP, which pg_hba
# gates behind scram-sha-256; the container runs as root, root has no password, so TCP
# can never authenticate and migrations die with "no password supplied". An empty host
# takes the unix socket, where peer auth accepts root.
[ -f .env ] || cp .env.example .env || { log "ERROR could not create .env"; exit 1; }
# Rewrite every run, not only on creation. A .env carried over from an earlier container
# keeps the localhost TCP form, which root can never authenticate against, and migrations
# die with "no password supplied" - the failure the comment above documents.
if true; then
  # python rather than `sed -i`: BSD sed reads -i's argument as a backup suffix, so this
  # silently left DATABASE_URL on the TCP form whenever it ran on a Mac - the exact
  # misconfiguration the comment above exists to prevent. python also avoids sed
  # replacement-syntax corruption from an & or a backslash inside the API key.
  DB="$DB" python3 -c '
import os, re, sys
p = ".env"
t = open(p).read()
t = re.sub(r"^DATABASE_URL=.*$", "DATABASE_URL=postgresql:///" + os.environ["DB"], t, flags=re.M)
key = os.environ.get("OPENAI_API_KEY")
if key:
    t, n = re.subn(r"^OPENAI_API_KEY=.*$", lambda m: "OPENAI_API_KEY=" + key, t, flags=re.M)
    if not n:
        t += "\nOPENAI_API_KEY=" + key + "\n"
open(p, "w").write(t)
' || { log "ERROR could not write .env"; exit 1; }

  if [ -z "${OPENAI_API_KEY:-}" ]; then
    log "WARN OPENAI_API_KEY is not set. Add it to the cloud environment's variables;"
    log "     without it, ingest and ask fail but tests and migrations still run."
  fi
fi

# ------------------------------------------------------------------- deps and schema
command -v uv >/dev/null 2>&1 || {
  log "ERROR uv is not installed. Install it first:"
  log "  curl -fsSL https://astral.sh/uv/install.sh | sh && export PATH=\"\$HOME/.local/bin:\$PATH\""
  exit 1; }
uv sync --extra dev  || { log "ERROR uv sync failed"; exit 1; }
uv run python scripts/migrate.py || { log "ERROR migrations failed"; exit 1; }

touch "$MARKER"
log "ready. Verify with: uv run python scripts/smoke_slice0.py"
