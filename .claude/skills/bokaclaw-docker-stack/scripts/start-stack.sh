#!/usr/bin/env bash
# Start Postgres + BokaClaw server + dashboard + sandboxed shell executor.
# Generates server/.env with random secrets when it is missing.
# Recreate the server/dashboard/executor containers with RECREATE=1 to pick up new images.
# The agent workspace is bind-mounted from $WORKSPACE_HOST and authoritative memory
# from $MEMORY_HOST so both survive re-creation and are visible on macOS.
# Shell tools reach the executor over the internal $EXECUTOR_NETWORK bridge; the
# executor has no other network, a read-only root filesystem and a non-root user.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../../../.." && pwd)
cd "$REPO_ROOT"

ENV_FILE=${ENV_FILE:-server/.env}
NETWORK=${NETWORK:-bokaclaw-net}
PG_CONTAINER=${PG_CONTAINER:-bokaclaw-postgres}
SERVER_CONTAINER=${SERVER_CONTAINER:-bokaclaw-server}
DASHBOARD_CONTAINER=${DASHBOARD_CONTAINER:-bokaclaw-dashboard}
EXECUTOR_CONTAINER=${EXECUTOR_CONTAINER:-bokaclaw-executor}
EVAL_CONTAINER=${EVAL_CONTAINER:-bokaclaw-eval}
EXECUTOR_NETWORK=${EXECUTOR_NETWORK:-bokaclaw-exec}
EXECUTOR_PORT=${EXECUTOR_PORT:-8099}
EXECUTOR_MEMORY=${EXECUTOR_MEMORY:-1g}
EXECUTOR_CPUS=${EXECUTOR_CPUS:-2}
EXECUTOR_PIDS_LIMIT=${EXECUTOR_PIDS_LIMIT:-256}
SERVER_IMAGE=${SERVER_IMAGE:-bokaclaw-server:test}
DASHBOARD_IMAGE=${DASHBOARD_IMAGE:-bokaclaw-dashboard:test}
EVAL_IMAGE=${EVAL_IMAGE:-bokaclaw-eval:test}
EXECUTOR_IMAGE=${EXECUTOR_IMAGE:-bokaclaw-executor:test}
EVAL_MEMORY=${EVAL_MEMORY:-2g}
EVAL_CPUS=${EVAL_CPUS:-2}
PG_IMAGE=${PG_IMAGE:-pgvector/pgvector:pg17}
PG_VOLUME=${PG_VOLUME:-mem0-local-pg}
HISTORY_VOLUME=${HISTORY_VOLUME:-mem0-local-history}
# Memory Markdown is authoritative: bind-mount it into the Mac host instead of
# keeping it inside the VM's Docker volume. Never mount it into the executor.
MEMORY_HOST=${MEMORY_HOST:-}
MEMORY_CONTAINER=${AGENT_MEMORY_ROOT:-/app/memory}
MEMORY_BACKEND=${MEMORY_BACKEND:-markdown}
PG_PORT=${PG_PORT:-8432}
API_PORT=${API_PORT:-8000}
DASHBOARD_PORT=${DASHBOARD_PORT:-3000}
API_PUBLIC_URL=${API_PUBLIC_URL:-http://localhost:${API_PORT}}
# Agent file tools write here; bind-mounted so files survive container re-creation.
WORKSPACE_HOST=${WORKSPACE_HOST:-/Users/pankaibo/BokaClaw/workspace}
# Global Agent Skills; read-only in the server container.
SKILLS_HOST=${SKILLS_HOST:-/Users/pankaibo/BokaClaw/skills}
# Optional MCP server definitions. Pass a directory (recommended) containing
# mcp.json, or a single JSON file. A directory is mounted at /app/config so
# edits are visible without recreating the container.
MCP_CONFIG_HOST=${MCP_CONFIG_HOST:-}
MCP_CONFIG_CONTAINER=${MCP_CONFIG_CONTAINER:-/app/config/mcp.json}

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

ADMIN_API_KEY=${ADMIN}
JWT_SECRET=${JWT}
AUTH_DISABLED=false
DASHBOARD_URL=http://localhost:${DASHBOARD_PORT}
APP_DB_NAME=mem0_app

MEMORY_DEFAULT_LLM_MODEL=gpt-5-mini
MEMORY_DEFAULT_EMBEDDER_PROVIDER=fastembed
MEMORY_DEFAULT_EMBEDDER_MODEL=BAAI/bge-small-zh-v1.5
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1

# Workspace Markdown is authoritative; Postgres holds a rebuildable index.
MEMORY_BACKEND=markdown
AGENT_MEMORY_ROOT=/app/memory
AGENT_MEMORY_RECALL_TOKENS=1500
AGENT_MEMORY_RECALL_MAX=15
AGENT_MEMORY_RECALL_THRESHOLD=0.4
AGENT_MEMORY_CAPTURE=true
AGENT_MEMORY_RECONCILE_ON_START=true
AGENT_MEMORY_RECONCILE_INTERVAL_SECONDS=300
AGENT_MEMORY_REQUIRE_APPROVAL=false

BOKACLAW_TELEMETRY=false
REQUEST_LOG_RETENTION_DAYS=30

AGENT_LOOP_MOCK=true
AGENT_LOOP_URL=
AGENT_LOOP_CONNECT_TIMEOUT_SECONDS=5
AGENT_LOOP_READ_TIMEOUT_SECONDS=120
AGENT_LOOP_MAX_SECONDS=300
AGENT_LOOP_HISTORY_MESSAGES=40

# Presentation tools (PptxBuild and friends); python-pptx is baked into the image.
AGENT_OFFICE_TOOLS=true

# Remote MCP servers over Streamable HTTP. Disabled by default. To use a JSON
# file, bind-mount it at this path (see references/env-and-volumes.md).
AGENT_MCP_ENABLED=false
# AGENT_MCP_CONFIG_FILE=/app/config/mcp.json

# Agent Skills (SKILL.md instruction packs). The host directory is bind-mounted
# read-only at AGENT_SKILLS_ROOT. P1 SkillRun scripts execute in the executor.
AGENT_SKILLS_ENABLED=false
AGENT_SKILLS_ROOT=/app/skills
AGENT_SKILLS_ALLOW_SCRIPTS=false

# Online evaluation worker (bokaclaw-eval). The judge is opt-in per run; the
# worker receives only DB credentials and these variables, never JWT_SECRET.
EVAL_ENABLED=true
EVAL_JUDGE_ENABLED=false
EVAL_JUDGE_PROVIDER=deepseek
EVAL_JUDGE_MODEL=deepseek-chat
EVAL_JUDGE_API_KEY=
EVAL_JUDGE_MAX_CHARS=20000
# Judge reasons and analysis are written in this language (zh or en).
EVAL_JUDGE_LANGUAGE=zh
EVAL_WORKER_ID=eval-1
EVAL_LEASE_SECONDS=60
EVAL_MAX_ATTEMPTS=3
EVAL_MAX_RUN_SECONDS=900
EVAL_RETENTION_DAYS=30
EOF
fi

# Shell tools are delegated to the executor container. Both variables live in
# .env so a manually started server container picks them up as well.
if ! grep -q '^AGENT_EXECUTOR_URL=' "$ENV_FILE"; then
  printf '\n# Sandboxed shell executor (Bash/PowerShell/Monitor).\nAGENT_EXECUTOR_URL=http://%s:%s\n' \
    "$EXECUTOR_CONTAINER" "$EXECUTOR_PORT" >> "$ENV_FILE"
fi
if ! grep -q '^AGENT_EXECUTOR_TOKEN=' "$ENV_FILE"; then
  printf 'AGENT_EXECUTOR_TOKEN=%s\n' "$(openssl rand -hex 24)" >> "$ENV_FILE"
fi
if ! grep -q '^AGENT_SKILLS_ROOT=' "$ENV_FILE"; then
  printf '\n# Agent Skills (SKILL.md instruction packs).\n' >> "$ENV_FILE"
  if ! grep -q '^AGENT_SKILLS_ENABLED=' "$ENV_FILE"; then
    printf 'AGENT_SKILLS_ENABLED=false\n' >> "$ENV_FILE"
  fi
  printf 'AGENT_SKILLS_ROOT=/app/skills\n' >> "$ENV_FILE"
fi
if ! grep -q '^AGENT_SKILLS_ALLOW_SCRIPTS=' "$ENV_FILE"; then
  printf 'AGENT_SKILLS_ALLOW_SCRIPTS=false\n' >> "$ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
. "$ENV_FILE"
set +a

# Prefer an explicit shell override, then .env (shared with docker-compose),
# then the local default. Memory must stay outside AGENT_WORKSPACE_ROOT.
MEMORY_HOST=${MEMORY_HOST:-${AGENT_MEMORY_HOST:-/Users/pankaibo/BokaClaw/memory}}
case "$MEMORY_HOST" in
  "$WORKSPACE_HOST" | "$WORKSPACE_HOST"/*)
    echo "error: MEMORY_HOST ($MEMORY_HOST) must not be inside WORKSPACE_HOST ($WORKSPACE_HOST)" >&2
    exit 1
    ;;
esac

POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_DB=${POSTGRES_DB:-postgres}
: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in $ENV_FILE}"
INSTANCE_NAME=${NEXT_PUBLIC_INSTANCE_NAME:-BokaClaw}
WORKSPACE_CONTAINER=${AGENT_WORKSPACE_ROOT:-/app/workspace}
SKILLS_CONTAINER=${AGENT_SKILLS_ROOT:-/app/skills}
mkdir -p "$WORKSPACE_HOST" "$SKILLS_HOST"

docker image inspect "$SERVER_IMAGE" >/dev/null 2>&1 || {
  echo "error: image $SERVER_IMAGE not found; run bokaclaw-docker-stack/scripts/build-images.sh first." >&2
  exit 1
}
docker image inspect "$EXECUTOR_IMAGE" >/dev/null 2>&1 || {
  echo "error: image $EXECUTOR_IMAGE not found; run bokaclaw-docker-stack/scripts/build-images.sh executor first." >&2
  exit 1
}
docker image inspect "$DASHBOARD_IMAGE" >/dev/null 2>&1 || {
  echo "error: image $DASHBOARD_IMAGE not found; run bokaclaw-docker-stack/scripts/build-images.sh first." >&2
  exit 1
}
docker image inspect "$EVAL_IMAGE" >/dev/null 2>&1 || {
  echo "error: image $EVAL_IMAGE not found; run bokaclaw-docker-stack/scripts/build-images.sh first." >&2
  exit 1
}
docker image inspect "$PG_IMAGE" >/dev/null 2>&1 || docker pull "$PG_IMAGE"

docker network inspect "$NETWORK" >/dev/null 2>&1 || {
  log "creating network $NETWORK"
  docker network create "$NETWORK" >/dev/null
}

# Internal bridge: the executor can talk to the server, nothing else.
docker network inspect "$EXECUTOR_NETWORK" >/dev/null 2>&1 || {
  log "creating internal network $EXECUTOR_NETWORK"
  docker network create --internal "$EXECUTOR_NETWORK" >/dev/null
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

# ---- Shell executor ---------------------------------------------------------
EXECUTOR_IMAGE_ID=$(docker image inspect -f '{{.Id}}' "$EXECUTOR_IMAGE")
if docker container inspect "$EXECUTOR_CONTAINER" >/dev/null 2>&1; then
  CURRENT_EXECUTOR_IMAGE=$(docker inspect -f '{{.Image}}' "$EXECUTOR_CONTAINER")
  if [ "${RECREATE:-0}" = "1" ] || [ "$CURRENT_EXECUTOR_IMAGE" != "$EXECUTOR_IMAGE_ID" ]; then
    log "recreating $EXECUTOR_CONTAINER (image changed or RECREATE=1)"
    docker rm -f "$EXECUTOR_CONTAINER" >/dev/null
  fi
fi

if docker container inspect "$EXECUTOR_CONTAINER" >/dev/null 2>&1; then
  docker start "$EXECUTOR_CONTAINER" >/dev/null
else
  log "starting $EXECUTOR_CONTAINER"
  docker run -d --name "$EXECUTOR_CONTAINER" --network "$EXECUTOR_NETWORK" --restart unless-stopped \
    --read-only --tmpfs /tmp:rw,size=256m,mode=1777 \
    --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit "$EXECUTOR_PIDS_LIMIT" --memory "$EXECUTOR_MEMORY" --cpus "$EXECUTOR_CPUS" \
    --user 1000:1000 -e HOME=/tmp \
    -e AGENT_DEV_TOOLS=true \
    -e AGENT_TOOLS_ALLOW_SHELL=true \
    -e AGENT_WORKSPACE_ROOT="$WORKSPACE_CONTAINER" \
    -e AGENT_TOOL_TIMEOUT_SECONDS="${AGENT_TOOL_TIMEOUT_SECONDS:-120}" \
    -e AGENT_EXECUTOR_TOKEN="$AGENT_EXECUTOR_TOKEN" \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "${WORKSPACE_HOST}:${WORKSPACE_CONTAINER}" \
    "$EXECUTOR_IMAGE" uvicorn app.agent.executor_app:app --host 0.0.0.0 --port "$EXECUTOR_PORT" --no-access-log >/dev/null
fi

log "waiting for executor"
executor_ready=0
for _ in $(seq 1 60); do
  if docker exec "$EXECUTOR_CONTAINER" python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:${EXECUTOR_PORT}/healthz', timeout=2)" \
    >/dev/null 2>&1; then
    executor_ready=1
    break
  fi
  sleep 0.5
done
if [ "$executor_ready" != "1" ]; then
  echo "error: $EXECUTOR_CONTAINER did not become ready" >&2
  docker logs --tail 40 "$EXECUTOR_CONTAINER" >&2
  exit 1
fi

# ---- Server ----------------------------------------------------------------
if [ "${RECREATE:-0}" = "1" ] && docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  log "recreating $SERVER_CONTAINER"
  docker rm -f "$SERVER_CONTAINER" >/dev/null
fi

# A container created before the executor existed has no executor env; recreate it.
if docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1 &&
  ! docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$SERVER_CONTAINER" |
    grep -q '^AGENT_EXECUTOR_URL=http'; then
  log "$SERVER_CONTAINER predates the shell executor; recreating"
  docker rm -f "$SERVER_CONTAINER" >/dev/null
fi

# A container created with the old named volume must switch to the host bind.
if docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  memory_mount=$(docker inspect -f \
    '{{range .Mounts}}{{.Destination}}={{if .Name}}{{.Name}}{{else}}{{.Source}}{{end}};{{end}}' \
    "$SERVER_CONTAINER")
  case "$memory_mount" in
    *"${MEMORY_CONTAINER}=${MEMORY_HOST};"*) ;;
    *)
      log "$SERVER_CONTAINER memory mount changed; recreating"
      docker rm -f "$SERVER_CONTAINER" >/dev/null
      ;;
  esac
fi

# A container created before skills were bind-mounted must pick up the mount.
if docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  skills_mount=$(docker inspect -f \
    '{{range .Mounts}}{{.Destination}}={{.Source}};{{end}}' "$SERVER_CONTAINER")
  case "$skills_mount" in
    *"${SKILLS_CONTAINER}=${SKILLS_HOST};"*) ;;
    *)
      log "$SERVER_CONTAINER skills mount changed; recreating"
      docker rm -f "$SERVER_CONTAINER" >/dev/null
      ;;
  esac
fi

MCP_MOUNT_ARGS=()
MCP_MOUNT_TARGET=""
if [ -n "$MCP_CONFIG_HOST" ]; then
  if [ -d "$MCP_CONFIG_HOST" ]; then
    MCP_MOUNT_TARGET=$(dirname "$MCP_CONFIG_CONTAINER")
    if [ ! -f "${MCP_CONFIG_HOST}/$(basename "$MCP_CONFIG_CONTAINER")" ]; then
      echo "error: ${MCP_CONFIG_HOST}/$(basename "$MCP_CONFIG_CONTAINER") does not exist" >&2
      exit 1
    fi
  elif [ -f "$MCP_CONFIG_HOST" ]; then
    MCP_MOUNT_TARGET="$MCP_CONFIG_CONTAINER"
  else
    echo "error: MCP_CONFIG_HOST=$MCP_CONFIG_HOST does not exist" >&2
    exit 1
  fi
  MCP_MOUNT_ARGS=(-v "${MCP_CONFIG_HOST}:${MCP_MOUNT_TARGET}:ro")
  grep -q '^AGENT_MCP_ENABLED=' "$ENV_FILE" ||
    printf '\n# MCP config mounted by start-stack.sh\nAGENT_MCP_ENABLED=true\n' >> "$ENV_FILE"
  grep -q '^AGENT_MCP_CONFIG_FILE=' "$ENV_FILE" ||
    printf 'AGENT_MCP_CONFIG_FILE=%s\n' "$MCP_CONFIG_CONTAINER" >> "$ENV_FILE"
  if ! grep -Eq '^AGENT_MCP_ENABLED=(true|1|yes|on)$' "$ENV_FILE"; then
    log "warning: AGENT_MCP_ENABLED is not true in $ENV_FILE; MCP tools stay disabled"
  fi
fi

if [ -n "$MCP_CONFIG_HOST" ] && docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  mcp_mount=$(docker inspect -f \
    '{{range .Mounts}}{{.Destination}}={{.Source}};{{end}}' "$SERVER_CONTAINER")
  case "$mcp_mount" in
    *"${MCP_MOUNT_TARGET}=${MCP_CONFIG_HOST};"*) ;;
    *)
      log "$SERVER_CONTAINER MCP config mount changed; recreating"
      docker rm -f "$SERVER_CONTAINER" >/dev/null
      ;;
  esac
fi

mkdir -p "$MEMORY_HOST"

if docker container inspect "$SERVER_CONTAINER" >/dev/null 2>&1; then
  docker start "$SERVER_CONTAINER" >/dev/null
else
  log "starting $SERVER_CONTAINER"
  docker run -d --name "$SERVER_CONTAINER" --network "$NETWORK" --restart unless-stopped \
    --env-file "$ENV_FILE" \
    -e POSTGRES_HOST="$PG_CONTAINER" -e POSTGRES_PORT=5432 \
    -e AGENT_EXECUTOR_URL="http://${EXECUTOR_CONTAINER}:${EXECUTOR_PORT}" \
    -e AGENT_EXECUTOR_TOKEN="$AGENT_EXECUTOR_TOKEN" \
    -e EVAL_ENABLED="${EVAL_ENABLED:-true}" \
    -e EVAL_LEASE_SECONDS="${EVAL_LEASE_SECONDS:-60}" \
    -e EVAL_MAX_ATTEMPTS="${EVAL_MAX_ATTEMPTS:-3}" \
    -e EVAL_JUDGE_ENABLED="${EVAL_JUDGE_ENABLED:-false}" \
    -e EVAL_JUDGE_PROVIDER="${EVAL_JUDGE_PROVIDER:-deepseek}" \
    -e EVAL_JUDGE_MODEL="${EVAL_JUDGE_MODEL:-deepseek-chat}" \
    -e EVAL_JUDGE_API_KEY="${EVAL_JUDGE_API_KEY:-}" \
    -e EVAL_JUDGE_LANGUAGE="${EVAL_JUDGE_LANGUAGE:-zh}" \
    -e FASTEMBED_CACHE_PATH=/app/history/fastembed \
    -e MEMORY_BACKEND="$MEMORY_BACKEND" \
    -e AGENT_MEMORY_ROOT="$MEMORY_CONTAINER" \
    --add-host host.docker.internal:host-gateway \
    -p "${API_PORT}:8000" \
    -v "${HISTORY_VOLUME}:/app/history" \
    -v "${MEMORY_HOST}:${MEMORY_CONTAINER}" \
    -v "${WORKSPACE_HOST}:${WORKSPACE_CONTAINER}" \
    -v "${SKILLS_HOST}:${SKILLS_CONTAINER}:ro" \
    ${MCP_MOUNT_ARGS[@]+"${MCP_MOUNT_ARGS[@]}"} \
    "$SERVER_IMAGE" uvicorn app.main:app --host 0.0.0.0 --port 8000 >/dev/null
fi

if ! docker inspect -f '{{json .NetworkSettings.Networks}}' "$SERVER_CONTAINER" |
  grep -q "\"${EXECUTOR_NETWORK}\""; then
  log "connecting $SERVER_CONTAINER to $EXECUTOR_NETWORK"
  docker network connect "$EXECUTOR_NETWORK" "$SERVER_CONTAINER" >/dev/null
fi

# ---- Eval worker ------------------------------------------------------------
# Least privilege: explicit DB credentials and EVAL_* only. Do not use
# --env-file here, or the worker would inherit JWT_SECRET / ADMIN_API_KEY /
# provider keys.
if [ "${RECREATE:-0}" = "1" ] && docker container inspect "$EVAL_CONTAINER" >/dev/null 2>&1; then
  log "recreating $EVAL_CONTAINER"
  docker rm -f "$EVAL_CONTAINER" >/dev/null
fi

if docker container inspect "$EVAL_CONTAINER" >/dev/null 2>&1; then
  docker start "$EVAL_CONTAINER" >/dev/null
else
  log "starting $EVAL_CONTAINER"
  docker run -d --name "$EVAL_CONTAINER" --network "$NETWORK" --restart unless-stopped \
    --read-only --tmpfs /tmp:rw,size=64m,mode=1777 --tmpfs /home/eval:rw,size=64m,mode=1777 \
    --cap-drop ALL --security-opt no-new-privileges \
    --pids-limit 256 --memory "$EVAL_MEMORY" --cpus "$EVAL_CPUS" \
    --user 1000:1000 \
    -e POSTGRES_HOST="$PG_CONTAINER" -e POSTGRES_PORT=5432 \
    -e POSTGRES_USER="$POSTGRES_USER" -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
    -e APP_DB_NAME="${APP_DB_NAME:-mem0_app}" \
    -e EVAL_ENABLED="${EVAL_ENABLED:-true}" \
    -e EVAL_WORKER_ID="${EVAL_WORKER_ID:-eval-1}" \
    -e EVAL_WORKER_POLL_SECONDS="${EVAL_WORKER_POLL_SECONDS:-2}" \
    -e EVAL_HEARTBEAT_SECONDS="${EVAL_HEARTBEAT_SECONDS:-10}" \
    -e EVAL_LEASE_SECONDS="${EVAL_LEASE_SECONDS:-60}" \
    -e EVAL_MAX_ATTEMPTS="${EVAL_MAX_ATTEMPTS:-3}" \
    -e EVAL_MAX_RUN_SECONDS="${EVAL_MAX_RUN_SECONDS:-900}" \
    -e EVAL_RETENTION_DAYS="${EVAL_RETENTION_DAYS:-30}" \
    -e EVAL_JUDGE_ENABLED="${EVAL_JUDGE_ENABLED:-false}" \
    -e EVAL_JUDGE_PROVIDER="${EVAL_JUDGE_PROVIDER:-deepseek}" \
    -e EVAL_JUDGE_MODEL="${EVAL_JUDGE_MODEL:-deepseek-chat}" \
    -e EVAL_JUDGE_API_KEY="${EVAL_JUDGE_API_KEY:-}" \
    -e EVAL_JUDGE_MAX_CHARS="${EVAL_JUDGE_MAX_CHARS:-20000}" \
    -e EVAL_JUDGE_LANGUAGE="${EVAL_JUDGE_LANGUAGE:-zh}" \
    "$EVAL_IMAGE" >/dev/null
fi

log "waiting for $EVAL_CONTAINER"
eval_ready=0
for _ in $(seq 1 90); do
  status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$EVAL_CONTAINER" 2>/dev/null || true)
  if [ "$status" = "healthy" ]; then
    eval_ready=1
    break
  fi
  sleep 2
done
if [ "$eval_ready" != "1" ]; then
  echo "error: $EVAL_CONTAINER did not become healthy" >&2
  docker logs --tail 40 "$EVAL_CONTAINER" >&2
  exit 1
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
printf '    Eval:      %s (worker %s)\n' "$EVAL_CONTAINER" "${EVAL_WORKER_ID:-eval-1}"
printf '    Workspace: %s -> %s\n' "$WORKSPACE_HOST" "$WORKSPACE_CONTAINER"
printf '    Memory:    bind %s -> %s\n' "$MEMORY_HOST" "$MEMORY_CONTAINER"
