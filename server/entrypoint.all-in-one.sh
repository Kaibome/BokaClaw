#!/bin/bash
set -euo pipefail

PG_BIN=/usr/lib/postgresql/17/bin
PGDATA=${PGDATA:-/var/lib/postgresql/data}
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_DB=${POSTGRES_DB:-postgres}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=${POSTGRES_PORT:-5432}
APP_DB_NAME=${APP_DB_NAME:-mem0_app}
AUTH_DISABLED=${AUTH_DISABLED:-false}

log() { printf '[mem0] %s\n' "$*"; }

# JWT_SECRET is only required when auth is enabled (server/main.py enforces the same).
if [ "${AUTH_DISABLED,,}" != "true" ] && [ -z "${JWT_SECRET:-}" ]; then
    log "JWT_SECRET must be set (openssl rand -base64 48), or set AUTH_DISABLED=true for local development."
    exit 1
fi
if ! [[ "$APP_DB_NAME" =~ ^[A-Za-z0-9_]+$ ]]; then
    log "APP_DB_NAME may only contain letters, digits and underscores."
    exit 1
fi

export POSTGRES_PASSWORD POSTGRES_USER POSTGRES_DB POSTGRES_HOST POSTGRES_PORT APP_DB_NAME
export API_INTERNAL_URL=${API_INTERNAL_URL:-http://127.0.0.1:8000}
export DASHBOARD_URL=${DASHBOARD_URL:-http://localhost:3000}
export NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL:-http://localhost:8000}
export NEXT_PUBLIC_INSTANCE_NAME=${NEXT_PUBLIC_INSTANCE_NAME:-BokaClaw}
export HISTORY_DB_PATH=${HISTORY_DB_PATH:-/app/history/history.db}

# Default to a CPU-only local embedder so the image works without an embedding
# API key. Override these for a different model or a cloud embedder.
export MEM0_DEFAULT_EMBEDDER_PROVIDER=${MEM0_DEFAULT_EMBEDDER_PROVIDER:-fastembed}
export MEM0_DEFAULT_EMBEDDER_MODEL=${MEM0_DEFAULT_EMBEDDER_MODEL:-BAAI/bge-small-zh-v1.5}
export MEM0_DEFAULT_EMBEDDING_DIMS=${MEM0_DEFAULT_EMBEDDING_DIMS:-512}

# ---- agent chat -----------------------------------------------------------
# POST /agent uses the bundled in-process runtime and the mem0 LLM config.
# AGENT_MODEL / AGENT_API_KEY / AGENT_BASE_URL override it for another
# OpenAI-compatible endpoint. AGENT_LOOP_URL optionally delegates the turn to
# an external executor instead.
export AGENT_MODEL=${AGENT_MODEL:-}
export AGENT_API_KEY=${AGENT_API_KEY:-}
export AGENT_BASE_URL=${AGENT_BASE_URL:-}
export AGENT_SYSTEM_PROMPT=${AGENT_SYSTEM_PROMPT:-}
export AGENT_MAX_ITERATIONS=${AGENT_MAX_ITERATIONS:-10}
export AGENT_TEMPERATURE=${AGENT_TEMPERATURE:-0.2}
export AGENT_MAX_TOKENS=${AGENT_MAX_TOKENS:-}
export AGENT_REQUEST_TIMEOUT_SECONDS=${AGENT_REQUEST_TIMEOUT_SECONDS:-60}
export AGENT_MAX_RETRIES=${AGENT_MAX_RETRIES:-2}
export AGENT_DEV_TOOLS=${AGENT_DEV_TOOLS:-true}
export AGENT_WORKSPACE_ROOT=${AGENT_WORKSPACE_ROOT:-/app/workspace}
export AGENT_TOOLS_ALLOW_SHELL=${AGENT_TOOLS_ALLOW_SHELL:-false}
export AGENT_TOOL_TIMEOUT_SECONDS=${AGENT_TOOL_TIMEOUT_SECONDS:-120}
export AGENT_TOOL_MAX_OUTPUT_BYTES=${AGENT_TOOL_MAX_OUTPUT_BYTES:-200000}
export AGENT_TOOL_MAX_FILE_BYTES=${AGENT_TOOL_MAX_FILE_BYTES:-2000000}
export AGENT_WEB_FETCH_TIMEOUT_SECONDS=${AGENT_WEB_FETCH_TIMEOUT_SECONDS:-30}
export AGENT_WEB_FETCH_MAX_BYTES=${AGENT_WEB_FETCH_MAX_BYTES:-1000000}
export AGENT_WEB_FETCH_ALLOW_PRIVATE=${AGENT_WEB_FETCH_ALLOW_PRIVATE:-false}
export AGENT_LOOP_URL=${AGENT_LOOP_URL:-}
export AGENT_LOOP_API_KEY=${AGENT_LOOP_API_KEY:-}
export AGENT_LOOP_MOCK=${AGENT_LOOP_MOCK:-false}
export AGENT_LOOP_CONNECT_TIMEOUT_SECONDS=${AGENT_LOOP_CONNECT_TIMEOUT_SECONDS:-5}
export AGENT_LOOP_READ_TIMEOUT_SECONDS=${AGENT_LOOP_READ_TIMEOUT_SECONDS:-120}
export AGENT_LOOP_MAX_SECONDS=${AGENT_LOOP_MAX_SECONDS:-300}
export AGENT_LOOP_HISTORY_MESSAGES=${AGENT_LOOP_HISTORY_MESSAGES:-40}

export HF_ENDPOINT=${HF_ENDPOINT:-https://hf-mirror.com}
# hf-mirror does not proxy Xet, which makes huggingface_hub fail with 401.
export HF_HUB_DISABLE_XET=${HF_HUB_DISABLE_XET:-1}

mkdir -p "$(dirname "$HISTORY_DB_PATH")"

psql_app() {
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" "$@"
}

# ---- PostgreSQL ------------------------------------------------------------
mkdir -p "$PGDATA" /var/run/postgresql
chown -R postgres:postgres "$PGDATA" /var/run/postgresql
chmod 700 "$PGDATA"

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    log "initializing PostgreSQL cluster in $PGDATA"
    pwfile=$(mktemp)
    printf '%s' "$POSTGRES_PASSWORD" > "$pwfile"
    chown postgres:postgres "$pwfile"
    chmod 600 "$pwfile"
    gosu postgres "$PG_BIN/initdb" \
        -D "$PGDATA" \
        -U "$POSTGRES_USER" \
        --pwfile="$pwfile" \
        --auth-local=trust \
        --auth-host=scram-sha-256 \
        --encoding=UTF8 \
        --locale=C.UTF-8 >/dev/null
    rm -f "$pwfile"
fi

log "starting PostgreSQL"
gosu postgres "$PG_BIN/postgres" -D "$PGDATA" \
    -c listen_addresses=127.0.0.1 \
    -c unix_socket_directories=/var/run/postgresql \
    -c logging_collector=off &
PG_PID=$!

for _ in $(seq 1 60); do
    if pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "$PG_PID" 2>/dev/null; then
        log "PostgreSQL exited during startup"
        exit 1
    fi
    sleep 1
done

if ! pg_isready -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" >/dev/null 2>&1; then
    log "PostgreSQL did not become ready in time"
    exit 1
fi

if [ "$(psql_app -tAc "SELECT 1 FROM pg_database WHERE datname='$APP_DB_NAME'")" != "1" ]; then
    log "creating database $APP_DB_NAME"
    psql_app -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$APP_DB_NAME\""
fi

psql_app -v ON_ERROR_STOP=1 -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null

# ---- schema migrations -----------------------------------------------------
log "running migrations"
cd /app
alembic upgrade head

# ---- dashboard runtime env ------------------------------------------------
# NEXT_PUBLIC_* values are inlined into .next/ at build time as the literal
# name of the variable; replace them with the real runtime values here.
cd /app/dashboard
for var in NEXT_PUBLIC_API_URL NEXT_PUBLIC_INSTANCE_NAME; do
    value="${!var}"
    escaped=$(printf '%s' "$value" | sed -e 's/[\\&|]/\\&/g')
    find .next -type f -exec sed -i "s|$var|$escaped|g" {} +
done

# ---- start services --------------------------------------------------------
log "starting dashboard on :3000 (browser API base: $NEXT_PUBLIC_API_URL)"
node /app/dashboard/server.js &
DASH_PID=$!

log "starting API on :8000"
cd /app
uvicorn main:app --host 0.0.0.0 --port 8000 &
API_PID=$!

cleanup() {
    log "shutting down"
    kill -TERM "$API_PID" "$DASH_PID" "$PG_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup TERM INT

set +e
wait -n "$API_PID" "$DASH_PID" "$PG_PID"
status=$?
set -e
log "a service exited (status $status); shutting down"
cleanup
exit "$status"
