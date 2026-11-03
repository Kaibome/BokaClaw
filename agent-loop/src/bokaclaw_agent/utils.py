"""Small internal helpers."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any


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
