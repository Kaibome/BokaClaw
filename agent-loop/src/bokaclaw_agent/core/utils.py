"""Small internal helpers."""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any


def parse_tool_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None, str | None]:
    """Normalize a provider's tool arguments into ``(object, raw, error)``.

    Returns an empty object and a human-readable error when the arguments are
    not a JSON object, so a malformed call can still be reported to the model.
    """

    if isinstance(raw_arguments, dict):
        return dict(raw_arguments), json.dumps(raw_arguments, ensure_ascii=False), None

    raw = raw_arguments if isinstance(raw_arguments, str) else ""
    if not raw.strip():
        return {}, raw, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, raw, str(exc)
    if not isinstance(parsed, dict):
        return {}, raw, "tool arguments must be a JSON object"
    return parsed, raw, None


def resolve_awaitable(value: Any) -> Any:
    """Return ``value``, running it to completion when it is awaitable.

    The runtime is synchronous. ``arun`` runs the whole loop in a worker
    thread, so ``asyncio.run`` is safe there and for plain ``run`` calls made
    outside an event loop. Calling blocking ``run`` from inside a running loop
    with an async tool raises a clear error; use ``await runtime.arun(...)``.
    """

    if not inspect.isawaitable(value):
        return value

    async def _await() -> Any:
        return await value

    try:
        return asyncio.run(_await())
    except RuntimeError as exc:  # pragma: no cover - depends on caller context
        if "running event loop" in str(exc):
            raise RuntimeError(
                "Cannot resolve an async tool/provider inside an active event loop; await runtime.arun(...) instead"
            ) from exc
        raise
