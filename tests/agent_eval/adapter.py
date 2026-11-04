"""Compatibility shim for the span adapter.

The canonical implementation moved to ``server/app/eval/adapter.py`` so the
worker image can ship it. Tests and the offline CLI keep importing this module.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parents[2] / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from app.eval.adapter import (  # noqa: E402,F401
    DEEPEVAL_VERSION,
    build_trace_dict,
    load_run,
    load_run_spans,
    require_deepeval,
    span_to_trace_dict,
    to_llm_test_case,
)
