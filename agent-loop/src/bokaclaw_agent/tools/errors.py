"""Classify tool failures so dispatch can decide whether a retry can help.

Tools raise plain exceptions; this module turns them into a small, stable
classification at the boundary. Only ``transient`` failures may be retried, and
``unknown`` is deliberately treated as non-retryable: a wrong retry multiplies
side effects, a missed one only costs a model round trip.
"""

from __future__ import annotations

import errno
import subprocess
import urllib.error
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from ..core.exceptions import (
    ToolArgumentError,
    ToolExecutionError,
    TransientToolError,
)

ErrorKind = Literal["transient", "permanent", "unknown"]

# Socket-level failures that are worth another attempt. Everything else in
# ``OSError`` (bad path, permission, disk full) is deterministic for the run.
_TRANSIENT_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)

# Argument/data problems: the same call will fail the same way.
_PERMANENT_EXCEPTIONS = (
    ToolArgumentError,
    ValueError,
    TypeError,
    KeyError,
    AttributeError,
    NotImplementedError,
    PermissionError,
    FileNotFoundError,
    IsADirectoryError,
    NotADirectoryError,
)


@dataclass(frozen=True, slots=True)
class ErrorClass:
    """One classified tool failure."""

    kind: ErrorKind
    retry_after: float | None = None
    cause_type: str = ""


def _retry_after(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0:
        return None
    return float(value)


@lru_cache(maxsize=1)
def _httpx_transient_types() -> tuple[type[BaseException], ...]:
    """Return the transient httpx exception types, or ``()`` when not installed."""

    try:
        import httpx
    except ImportError:  # pragma: no cover - httpx is a server dependency, optional here
        return ()
    timeout = getattr(httpx, "TimeoutException", None)
    network = getattr(httpx, "NetworkError", None)
    if timeout is not None and network is not None and isinstance(timeout, type) and isinstance(network, type):
        return (timeout, network)
    # Older httpx releases only expose the broader TransportError.
    transport = getattr(httpx, "TransportError", None)
    return (transport,) if isinstance(transport, type) else ()


def classify_tool_error(exc: BaseException) -> ErrorClass:
    """Classify ``exc`` and return its kind plus any tool-supplied retry delay."""

    if isinstance(exc, TransientToolError):
        return ErrorClass("transient", _retry_after(exc.retry_after), type(exc).__name__)

    if isinstance(exc, ToolExecutionError):
        # Already classified at a lower boundary; keep that decision.
        if exc.kind != "unknown":
            return ErrorClass(exc.kind, _retry_after(exc.retry_after), exc.cause_type or type(exc).__name__)
        if isinstance(exc.__cause__, BaseException):
            classified = classify_tool_error(exc.__cause__)
            return ErrorClass(classified.kind, classified.retry_after, classified.cause_type or type(exc).__name__)
        return ErrorClass("unknown", None, type(exc).__name__)

    if isinstance(exc, _PERMANENT_EXCEPTIONS):
        return ErrorClass("permanent", None, type(exc).__name__)

    if isinstance(exc, urllib.error.HTTPError):
        status = exc.code if isinstance(exc.code, int) else 0
        kind: ErrorKind = "transient" if status == 429 or 500 <= status < 600 else "permanent"
        return ErrorClass(kind, None, type(exc).__name__)

    if isinstance(exc, urllib.error.URLError):
        return ErrorClass("transient", None, type(exc).__name__)

    if isinstance(exc, TimeoutError):
        return ErrorClass("transient", None, type(exc).__name__)

    if isinstance(exc, subprocess.TimeoutExpired):
        return ErrorClass("transient", None, type(exc).__name__)

    if isinstance(exc, ConnectionError):
        return ErrorClass("transient", None, type(exc).__name__)

    if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
        return ErrorClass("transient", None, type(exc).__name__)

    transient_httpx = _httpx_transient_types()
    if transient_httpx and isinstance(exc, transient_httpx):
        return ErrorClass("transient", None, type(exc).__name__)

    return ErrorClass("unknown", None, type(exc).__name__)
