"""Prune terminal agent runs and their event logs.

Events are deleted after AGENT_RUN_EVENT_RETENTION_DAYS (default 7) and the run
row itself after AGENT_RUN_RETENTION_DAYS (default 30). Only terminal runs are
touched, so an active run can never lose its events mid-stream.

Run inside the `mem0` container, or wire into cron / a systemd timer on the host.
"""

import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


from app.agent.repo import RunRepository  # noqa: E402
from app.agent.runner import load_run_config  # noqa: E402


def main() -> int:
    config = load_run_config()
    repo = RunRepository()
    events = repo.prune_events(retention_days=config.event_retention_days)
    runs = repo.prune_runs(retention_days=config.run_retention_days)
    sys.stdout.write(
        f"Deleted {events} agent_run_events rows older than {config.event_retention_days} days "
        f"and {runs} agent_runs rows older than {config.run_retention_days} days.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
