"""Evaluate persisted agent runs with deepeval.

Example:
    python tests/agent_eval/run.py --run-id <uuid> --goldens tests/agent_eval/goldens.example.json

Run this from the evaluation environment (``hatch run eval:test``, or any venv
where ``pip install -e ./deepeval`` has been run), not the server environment:
deepeval is built from the in-repo source and requires a newer pydantic than the
server pins. Nothing is uploaded; results are printed to stdout.

The implementation lives in ``server/app/eval/cli.py`` so the worker image and
the offline CLI share one evaluator. This file only sets up the import path.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "1")

_SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.eval.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
