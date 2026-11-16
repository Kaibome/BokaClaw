"""Process-wide per-tool circuit breakers.

State is keyed by tool name and shared by every runtime in the process: a tool
that is down is down for everyone, so each run should not rediscover it. The
breaker only counts failures classified ``transient``; a bad argument or a
permission error never opens it.

    closed --(threshold consecutive transient failures)--> open
    open --(cooldown elapsed)--> half_open (one probe at a time)
    half_open --(probe succeeds)--> closed
    half_open --(probe fails transients)--> open, cooldown doubles (capped)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True, slots=True)
class BreakerSettings:
    """Per-runtime breaker tuning; state itself is process-wide."""

    failure_threshold: int = 5
    cooldown_seconds: float = 60.0
    max_cooldown_seconds: float = 600.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must not be negative")
        if self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError("max_cooldown_seconds must be greater than or equal to cooldown_seconds")


@dataclass(slots=True)
class _BreakerState:
    failures: int = 0
    opened_at: float | None = None
    cooldown: float = 0.0
    probe_in_flight: bool = False


class ToolCircuitBreaker:
    """Thread-safe breaker registry shared by every tool registry in a process."""

    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._states: dict[str, _BreakerState] = {}

    def allow(self, name: str, settings: BreakerSettings) -> tuple[bool, float]:
        """Return ``(allowed, seconds_to_wait)`` for the next call of ``name``.

        A half-open breaker admits exactly one probe; concurrent callers get a
        fast rejection until that probe reports back.
        """

        with self._lock:
            state = self._states.get(name)
            if state is None or state.opened_at is None:
                return True, 0.0
            elapsed = self._clock() - state.opened_at
            if elapsed >= state.cooldown:
                if state.probe_in_flight:
                    return False, 0.0
                state.probe_in_flight = True
                return True, 0.0
            return False, max(0.0, state.cooldown - elapsed)

    def record_success(self, name: str) -> None:
        with self._lock:
            self._states.pop(name, None)

    def record_failure(self, name: str, settings: BreakerSettings) -> None:
        with self._lock:
            state = self._states.get(name)
            if state is None:
                state = _BreakerState(cooldown=settings.cooldown_seconds)
                self._states[name] = state
            if state.opened_at is not None:
                # A failed half-open probe reopens with a longer cooldown so a
                # permanently broken dependency is probed less and less.
                state.probe_in_flight = False
                state.cooldown = min(settings.max_cooldown_seconds, max(settings.cooldown_seconds, state.cooldown * 2))
                state.opened_at = self._clock()
                return
            state.failures += 1
            if state.failures >= settings.failure_threshold:
                state.opened_at = self._clock()
                state.cooldown = settings.cooldown_seconds

    def record_ignored(self, name: str) -> None:
        """Release a half-open probe that ended in a non-transient outcome.

        The tool responded (even with a permanent error), so it is reachable;
        the closed-state failure counter is left untouched.
        """

        with self._lock:
            state = self._states.get(name)
            if state is not None and state.opened_at is not None and state.probe_in_flight:
                self._states.pop(name, None)

    def state(self, name: str) -> str:
        with self._lock:
            state = self._states.get(name)
            if state is None or state.opened_at is None:
                return "closed"
            return "half_open" if self._clock() - state.opened_at >= state.cooldown else "open"

    def reset(self) -> None:
        with self._lock:
            self._states.clear()


_BREAKER = ToolCircuitBreaker()


def get_tool_breaker() -> ToolCircuitBreaker:
    """Return the process-wide breaker shared by every registry."""

    return _BREAKER


def reset_tool_breakers() -> None:
    """Clear every breaker state; intended for tests and administrative resets."""

    _BREAKER.reset()
