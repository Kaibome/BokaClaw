#!/usr/bin/env bash
# Start Postgres + BokaClaw server + dashboard as three containers.
# Generates server/.env with random secrets when it is missing.
# Recreate the server/dashboard containers with RECREATE=1 to pick up new images.
# The agent workspace is bind-mounted from $WORKSPACE_HOST so files survive re-creation.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../../../.." && pwd)
cd "$REPO_ROOT"

ENV_FILE=${ENV_FILE:-server/.env}
NETWORK=${NETWORK:-bokaclaw-net}
PG_CONTAINER=${PG_CONTAINER:-bokaclaw-postgres}
SERVER_CONTAINER=${SERVER_CONTAINER:-bokaclaw-server}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-bokaclaw-dashboard}
SERVER_IMAGE=${SERVER_IMAGE:-bokaclaw-server:test}
DASHBOARD_IMAGE=${DASHBOARD_IMAGE:-bokaclaw-dashboard:test}
PG_IMAGE=${PG_IMAGE:-pgvector/pgvector:pg17}
PG_VOLUME=${PG_VOLUME:-mem0-local-pg}
HISTORY_VOLUME=${HISTORY_VOLUME:-mem0-local-history}
PG_PORT=${PG_PORT:-8432}
API_PORT=${API_PORT:-8000}
DASHBOARD_PORT=${DASHBOARD_PORT:-3000}
API_PUBLIC_URL=${API_PUBLIC_URL:-http://localhost:${API_PORT}}
# Agent file tools write here; bind-mounted so files survive container re-creation.
WORKSPACE_HOST=${WORKSPACE_HOST:-/Users/pankaibo/BokaClaw/workspace}

log() { printf '==> %s\n' "$*"; }

if [ ! -f "$ENV_FILE" ]; then
  log "creating $ENV_FILE with random secrets"
  umask 077
  PGPW=$(openssl rand -hex 24)
  JWT=$(openssl rand -base64 48)
  ADMIN=$(openssl rand -hex 32)
  cat > "$ENV_FILE" <<EOF
OPENAI_API_KEY=

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=postgres
POSTGRES_USER=postgres
POSTGRES_PASSWORD=${PGPW}
POSTGRES_COLLECTION_NAME=memories

ADMIN_API_KEY=${ADMIN}
JWT_SECRET=${JWT}
AUTH_DISABLED=false
DASHBOARD_URL=http://localhost:${DASHBOARD_PORT}
APP_DB_NAME=mem0_app

MEM0_DEFAULT_LLM_MODEL=gpt-5-mini
MEM0_DEFAULT_EMBEDDER_PROVIDER=fastembed
MEM0_DEFAULT_EMBEDDER_MODEL=BAAI/bge-small-zh-v1.5
MEM0_DEFAULT_EMBEDDING_DIMS=512
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1

MEM0_TELEMETRY=false
REQUEST_LOG_RETENTION_DAYS=30

AGENT_LOOP_MOCK=true
AGENT_LOOP_URL=
AGENT_LOOP_CONNECT_TIMEOUT_SECONDS=5
AGENT_LOOP_READ_TIMEOUT_SECONDS=120
AGENT_LOOP_MAX_SECONDS=300
AGENT_LOOP_HISTORY_MESSAGES=40
EOF
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_DB=${POSTGRES_DB:-postgres}
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in $ENV_FILE}"
INSTANCE_NAME=${NEXT_PUBLIC_INSTANCE_NAME:-BokaClaw}
WORKSPACE_CONTAINER=${AGENT_WORKSPACE_ROOT:-/app/workspace}
mkdir -p "$WORKSPACE_HOST"

docker image inspect "$SERVER_IMAGE" >/dev/null 2>&1 || {
  echo "error: image $SERVER_IMAGE not found; run bokaclaw-docker-stack/scripts/build-images.sh first." >&2
  exit 1
}
docker image inspect "$DASHBOARD_IMAGE" >/dev/null 2>&1 || {
  echo "error: image $DASHBOARD_IMAGE not found; run bokaclaw-docker-stack/scripts/build-images.sh first." >&2
  exit 1
}
docker image inspect "$PG_IMAGE" >/dev/null 2>&1 || docker pull "$PG_IMAGE"

docker network inspect "$NETWORK" >/dev/null 2>&1 || {
  log "creating network $NETWORK"
  docker network create "$NETWORK" >/dev/null
}

# ---- Postgres --------------------------------------------------------------
if docker container inspect "$PG_CONTAINER" >/dev/null 2>&1; then
  docker start "$PG_CONTAINER" >/dev/null
else
  log "starting $PG_CONTAINER (volume $PG_VOLUME)"
  docker run -d --name "$PG_CONTAINER" --network "$NETWORK" --restart unless-stopped \
    -e POSTGRES_USER="$POSTGRES_USER" \
    -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -e POSTGRES_DB="$POSTGRES_DB" \
    -p "${PG_PORT}:5432" \
    -v "${PG_VOLUME}:/var/lib/postgresql/data" \
    "$PG_IMAGE" postgres -c listen_addresses='*' >/dev/null
fi

log "waiting for Postgres"
until docker exec "$PG_CONTAINER" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; do
  sleep 1
done

# The reused volume predates this .env, so make .env the source of truth.
escaped_pw=${POSTGRES_PASSWORD//\'/\'\'}
docker exec -u postgres "$PG_CONTAINER" psql -U "$POSTGRES_USER" -q -v ON_ERROR_STOP=1 \
  -c "ALTER USER \"$POSTGRES_USER\" PASSWORD '$escaped_pw'" >/dev/null

# The all-in-one volume only allowed 127.0.0.1; containers arrive on the bridge.
docker exec -u postgres "$PG_CONTAINER" sh -c '
  conf="${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf"
  grep -qE "^host +all +all +0\.0\.0\.0/0" "$conf" ||
    printf "\n# allow containers on the bokaclaw bridge\nhost all all 0.0.0.0/0 scram-sha-256\nhost all all ::/0 scram-sha-256\n" >> "$conf"
'
docker exec -u postgres "$PG_CONTAINER" psql -U "$POSTGRES_USER" -tAc "SELECT pg_reload_conf()" >/dev/null

# ---- Migrations ------------------------------------------------------------
log "running alembic upgrade head"
docker run --rm --network "$NETWORK" --env-file "$ENV_FILE" \
  -e POSTGRES_HOST="$PG_CONTAINER" -e POSTGRES_PORT=5432 \
  -v "${HISTORY_VOLUME}:/app/history" \
  "$SERVER_IMAGE" alembic upgrade head

# ---- Server ----------------------------------------------------------------
if [ "${RECREATE:-0}" = "1" ] && docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  log "recreating $SERVER_CONTAINER"
  docker rm -f "$SERVER_CONTAINER" >/dev/null
fi

if docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  docker start "$SERVER_CONTAINER" >/dev/null
else
  log "starting $SERVER_CONTAINER"
  docker run -d --name "$SERVER_CONTAINER" --network "$NETWORK" --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -e POSTGRES_HOST="$PG_CONTAINER" -e POSTGRES_PORT=5432 \
    --add-host host.docker.internal:host-gateway \
    -p "${API_PORT}:8000" \
    -v "${HISTORY_VOLUME}:/app/history" \
    -v "${WORKSPACE_HOST}:${WORKSPACE_CONTAINER}" \
    "$SERVER_IMAGE" uvicorn main:app --host 0.0.0.0 --port 8000 >/dev/null
fi

# ---- Dashboard -------------------------------------------------------------
if [ "${RECREATE:-0}" = "1" ] && docker container inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1; then
  log "recreating $DASHBOARD_CONTAINER"
  docker rm -f "$DASHBOARD_CONTAINER" >/dev/null
fi

if docker container inspect "$DASHBOARD_CONTAINER" >/dev/null 2>&1; then
  docker start "$DASHBOARD_CONTAINER" >/dev/null
else
  log "starting $DASHBOARD_CONTAINER"
  docker run -d --name "$DASHBOARD_CONTAINER" --network "$NETWORK" --restart unless-stopped \
    -e NEXT_PUBLIC_API_URL="$API_PUBLIC_URL" \
    -e API_INTERNAL_URL="http://${SERVER_CONTAINER}:8000" \
    -e NEXT_PUBLIC_INSTANCE_NAME="$INSTANCE_NAME" \
    -p "${DASHBOARD_PORT}:3000" \
    "$DASHBOARD_IMAGE" >/dev/null
fi

# ---- Health ----------------------------------------------------------------
wait_for() {
  local url=$1 name=$2 attempts=${3:-180}
  log "waiting for $name ($url)"
  for _ in $(seq 1 "$attempts"); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      return 0
    fi
    sleep 2
  done
  echo "error: $name did not become ready at $url" >&2
  return 1
}

wait_for "http://localhost:${API_PORT}/openapi.json" "API"
wait_for "http://localhost:${DASHBOARD_PORT}/api/health" "dashboard"

log "stack is ready"
printf '    Dashboard: %s\n' "http://localhost:${DASHBOARD_PORT}"
printf '    API:       %s\n' "$API_PUBLIC_URL"
printf '    Postgres:  localhost:%s\n' "$PG_PORT"
printf '    Workspace: %s -> %s\n' "$WORKSPACE_HOST" "$WORKSPACE_CONTAINER"
