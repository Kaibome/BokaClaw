"""Delete uploaded attachments that no conversation message references.

Attachment ids are content hashes stored under AGENT_ATTACHMENTS_ROOT; there is
no database row to cascade-delete. This script scans all user directories and
removes images (and their JSON sidecars) older than 24 hours that no message's
metadata.attachments points at.

Run inside the `bokaclaw-server` container, or wire it into cron / a timer.
"""

import os
import sys

_SERVER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)


from app.agent.attachments import prune_orphans  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402


def main() -> int:
    with SessionLocal() as db:
        stats = prune_orphans(db)
    sys.stdout.write(
        f"Deleted {stats['deleted']} orphaned attachment files; kept {stats['kept']} referenced or recent files.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
