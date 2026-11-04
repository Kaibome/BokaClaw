#!/usr/bin/env bash
# Build the BokaClaw images with fast mirrors, then smoke-test them.
# Usage: scripts/build-images.sh [server|dashboard|eval|all]   (default: all)
set -uo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../../../.." && pwd)
cd "$REPO_ROOT"

PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmmirror.com}
SERVER_IMAGE=${SERVER_IMAGE:-bokaclaw-server:test}
DASHBOARD_IMAGE=${DASHBOARD_IMAGE:-bokaclaw-dashboard:test}
EVAL_IMAGE=${EVAL_IMAGE:-bokaclaw-eval:test}
TARGET=${1:-all}

server_status="skipped"
dashboard_status="skipped"
eval_status="skipped"

if [ "$TARGET" = "all" ] || [ "$TARGET" = "server" ]; then
  echo "==> building $SERVER_IMAGE (context: repo root, pip: $PIP_INDEX_URL)"
  docker build \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    -f server/Dockerfile \
    -t "$SERVER_IMAGE" .
  server_status=$?
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "dashboard" ]; then
  echo "==> building $DASHBOARD_IMAGE (context: dashboard/, npm: $NPM_REGISTRY)"
  docker build \
    --build-arg NPM_REGISTRY="$NPM_REGISTRY" \
    -t "$DASHBOARD_IMAGE" dashboard
  dashboard_status=$?
fi

if [ "$TARGET" = "all" ] || [ "$TARGET" = "eval" ]; then
  echo "==> building $EVAL_IMAGE (context: repo root, pip: $PIP_INDEX_URL)"
  docker build \
    --build-arg PIP_INDEX_URL="$PIP_INDEX_URL" \
    -f server/eval.Dockerfile \
    -t "$EVAL_IMAGE" .
  eval_status=$?
fi

echo "==> results: server=$server_status dashboard=$dashboard_status eval=$eval_status"

fail=0
if [ "$server_status" = "0" ]; then
  # Importing app.main needs a full runtime env (JWT_SECRET, model credentials,
  # a reachable Postgres); compile the package instead and import the modules
  # that have no import-time side effects.
  docker run --rm --entrypoint python "$SERVER_IMAGE" \
    -c 'import compileall, sys; sys.exit(0 if compileall.compile_dir("/app/app", quiet=1) else 1)' || fail=1
  docker run --rm --entrypoint python "$SERVER_IMAGE" \
    -c 'import fastapi, httpx, sqlalchemy, alembic, bokaclaw_agent, app.agent.executor, app.core.auth; print("server-ok")' || fail=1
elif [ "$server_status" != "skipped" ]; then
  fail=1
fi

if [ "$dashboard_status" = "0" ]; then
  docker run --rm --entrypoint sh "$DASHBOARD_IMAGE" \
    -c 'test -f /app/server.js && test -d /app/.next/static && echo dashboard-ok' || fail=1
elif [ "$dashboard_status" != "skipped" ]; then
  fail=1
fi

if [ "$eval_status" = "0" ]; then
  # deepeval must resolve inside the isolated image and stay out of the server.
  docker run --rm --entrypoint python "$EVAL_IMAGE" \
    -c 'import deepeval, app.eval.worker, app.eval.healthcheck; print("eval-ok")' || fail=1
elif [ "$eval_status" != "skipped" ]; then
  fail=1
fi

exit "$fail"
