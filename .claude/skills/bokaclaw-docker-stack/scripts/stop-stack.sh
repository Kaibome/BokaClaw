#!/usr/bin/env bash
# Stop (or remove) the local BokaClaw containers. Volumes are never touched.
# Usage: stop-stack.sh [--remove]
set -euo pipefail

REMOVE=0
if [ "${1:-}" = "--remove" ]; then
  REMOVE=1
fi

CONTAINERS=${CONTAINERS:-"bokaclaw-dashboard bokaclaw-server bokaclaw-eval bokaclaw-executor bokaclaw-postgres"}

for name in $CONTAINERS; do
  if ! docker container inspect "$name" >/dev/null 2>&1; then
    echo "==> $name: not present"
    continue
  fi
  if [ "$REMOVE" = "1" ]; then
    echo "==> removing $name"
    docker rm -f "$name" >/dev/null
  else
    echo "==> stopping $name"
    docker stop "$name" >/dev/null
  fi
done

echo "Volumes (mem0-local-pg, mem0-local-history) were not touched."
