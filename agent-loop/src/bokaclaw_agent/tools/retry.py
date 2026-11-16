"""Bounded retry policy for tool calls."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Protocol


class _RandomSource(Protocol):
    def uniform(self, a: float, b: float) -> float:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often and how long one tool call may be retried.

    ``max_attempts`` counts the first call. Only failures classified as
    ``transient`` are retried, and only for tools that opt in (see
    ``Tool.retry``). ``max_elapsed`` caps the time spent on retries, including
    the backoff waits, so a flaky dependency cannot stall the whole run.
    """

    max_attempts: int = 3
    base_delay: float = 0.4
    max_delay: float = 8.0
    max_elapsed: float = 15.0
    jitter: float = 0.3

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0:
            raise ValueError("base_delay must not be negative")
        if self.max_delay < 0:
            raise ValueError("max_delay must not be negative")
        if self.max_elapsed < 0:
            raise ValueError("max_elapsed must not be negative")
        if not 0 <= self.jitter <= 1:
            raise ValueError("jitter must be between 0 and 1")


def backoff_delay(
    attempt: int,
    policy: RetryPolicy,
    retry_after: float | None = None,
    *,
    rng: _RandomSource | None = None,
) -> float:
    """Return the wait before the attempt after ``attempt`` (1-based) failed.

    The base delay doubles per attempt, is capped by ``max_delay``, and is
    reduced by up to ``jitter`` so concurrent clients do not retry in lockstep.
    A tool-supplied ``retry_after`` (for example HTTP ``Retry-After``) is a
    lower bound and may exceed ``max_delay``.
    """

    if attempt < 1:
        raise ValueError("attempt must be at least 1")
    source = rng if rng is not None else random
    delay = min(policy.max_delay, policy.base_delay * (2 ** (attempt - 1)))
    if policy.jitter:
        delay *= source.uniform(1.0 - policy.jitter, 1.0)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return max(0.0, delay)
