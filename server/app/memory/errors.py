"""Errors raised by the workspace-Markdown memory backend."""

from __future__ import annotations


class MemoryErrorBase(Exception):
    """Base class for memory backend errors."""


class MemoryValidationError(MemoryErrorBase, ValueError):
    """The caller supplied an invalid memory request."""


class MemoryNotFound(MemoryErrorBase, LookupError):
    """The requested memory entry does not exist."""


class MemoryBusy(MemoryErrorBase, RuntimeError):
    """A workspace lock could not be acquired in time."""


class MemoryUnavailable(MemoryErrorBase, RuntimeError):
    """The memory backend or one of its providers is unavailable."""
