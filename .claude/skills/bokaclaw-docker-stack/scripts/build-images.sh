#!/usr/bin/env bash
# Build both BokaClaw images with fast mirrors, then smoke-test them.
# Usage: scripts/build-images.sh [server|dashboard|all]   (default: all)
set -uo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../../../.." && pwd)
cd "$REPO_ROOT"

PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
NPM_REGISTRY=${NPM_REGISTRY:-https://registry.npmmirror.com}
SERVER_IMAGE=${SERVER_IMAGE:-bokaclaw-server:test}
DASHBOARD_IMAGE=${DASHBOARD_IMAGE:-bokaclaw-dashboard:test}
TARGET=${1:-all}

server_status="skipped"
dashboard_status="skipped"

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

echo "==> results: server=$server_status dashboard=$dashboard_status"

fail=0
if [ "$server_status" = "0" ]; then
  docker run --rm --entrypoint python "$SERVER_IMAGE" \
    -c 'import fastapi, httpx, sqlalchemy, alembic, bokaclaw_agent; print("server-ok")' || fail=1
elif [ "$server_status" != "skipped" ]; then
  fail=1
fi

if [ "$dashboard_status" = "0" ]; then
  docker run --rm --entrypoint sh "$DASHBOARD_IMAGE" \
    -c 'test -f /app/server.js && test -d /app/.next/static && echo dashboard-ok' || fail=1
elif [ "$dashboard_status" != "skipped" ]; then
  fail=1
fi

exit "$fail"
