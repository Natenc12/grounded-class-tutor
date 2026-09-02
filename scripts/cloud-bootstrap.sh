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
# Idempotent, and near-instant once the container has been through it.

set -uo pipefail
MARKER="$HOME/.gct-cloud-bootstrapped"
DB=grounded_class_tutor
log() { printf '[gct] %s\n' "$*"; }

if [ -f "$MARKER" ] && pg_isready -q 2>/dev/null; then
  log "already bootstrapped"; exit 0
fi

# --------------------------------------------------------------- postgres + pgvector
if ! command -v pg_isready >/dev/null 2>&1; then
  log "no postgres binaries on PATH; looking for a cluster install"
  export PATH="/usr/lib/postgresql/16/bin:$PATH"
fi

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
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^DATABASE_URL=.*|DATABASE_URL=postgresql:///$DB|" .env
  if [ -n "${OPENAI_API_KEY:-}" ]; then
    sed -i "s|^OPENAI_API_KEY=.*|OPENAI_API_KEY=$OPENAI_API_KEY|" .env
  else
    log "WARN OPENAI_API_KEY is not set. Add it to the cloud environment's variables;"
    log "     without it, ingest and ask fail but tests and migrations still run."
  fi
fi

# ------------------------------------------------------------------- deps and schema
command -v uv >/dev/null 2>&1 || { log "ERROR uv missing; claude-home's session hook installs it"; exit 1; }
uv sync --extra dev  || { log "ERROR uv sync failed"; exit 1; }
uv run python scripts/migrate.py || { log "ERROR migrations failed"; exit 1; }

touch "$MARKER"
log "ready. Verify with: uv run python scripts/smoke_slice0.py"
