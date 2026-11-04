"""Container health check for the eval worker.

Healthy means the database answers and the worker refreshed its heartbeat file
recently. The file is touched once per poll cycle, so a wedged loop fails.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from sqlalchemy import text

from app.core.db import engine


def main() -> int:
    health_file = Path(os.environ.get("EVAL_HEALTH_FILE", "/tmp/eval-worker-heartbeat"))
    max_age = float(os.environ.get("EVAL_HEALTH_MAX_AGE", "120"))
    try:
        age = time.time() - health_file.stat().st_mtime
    except OSError:
        print(f"eval-worker: heartbeat file {health_file} is missing")
        return 1
    if age > max_age:
        print(f"eval-worker: heartbeat is {age:.0f}s old (limit {max_age:.0f}s)")
        return 1
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - health checks must not raise
        print(f"eval-worker: database unreachable: {exc}")
        return 1
    print(f"eval-worker-ok (heartbeat {age:.0f}s old)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
