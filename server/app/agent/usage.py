"""Per-run model token accounting.

One :class:`RunUsageMeter` is created per claimed run. The agent-loop emits one
``AgentUsageUpdate`` per model call, and the memory embedding/LLM adapters record
through the ``CURRENT_METER`` context variable. The meter never fabricates
tokens: a call that returns no usage is counted as ``unknown_calls``.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator, Mapping

logger = logging.getLogger(__name__)

SOURCES = ("agent_loop", "compression", "memory_llm", "memory_embedding", "subagents")
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_TOKEN_ALIASES = {
    "prompt_tokens": "prompt_tokens",
    "completion_tokens": "completion_tokens",
    "total_tokens": "total_tokens",
    "input_tokens": "prompt_tokens",
    "output_tokens": "completion_tokens",
    "input": "prompt_tokens",
    "output": "completion_tokens",
    "total": "total_tokens",
    "cached_tokens": "cached_tokens",
    "prompt_cache_hit_tokens": "cached_tokens",
    "cache_miss_tokens": "cache_miss_tokens",
    "prompt_cache_miss_tokens": "cache_miss_tokens",
    "reasoning_tokens": "reasoning_tokens",
}

CURRENT_METER: ContextVar["RunUsageMeter | None"] = ContextVar("bokaclaw_run_usage_meter", default=None)


def normalize_usage(usage: Any) -> dict[str, int]:
    """Return canonical ``prompt_tokens``/``completion_tokens``/``total_tokens``.

    Accepts dicts, pydantic models (the OpenAI SDK usage object), and plain
    objects. Unknown or non-numeric fields are ignored.
    """

    if isinstance(usage, Mapping):
        source: Mapping[str, Any] = usage
    else:
        dump = getattr(usage, "model_dump", None)
        if callable(dump):
            try:
                dumped = dump()
            except Exception:  # pragma: no cover - defensive
                return {}
            if not isinstance(dumped, Mapping):
                return {}
            source = dumped
        elif usage is not None:
            source = {key: getattr(usage, key, None) for key in _TOKEN_ALIASES}
        else:
            return {}

    values: dict[str, int] = {}
    for key, value in source.items():
        canonical = _TOKEN_ALIASES.get(str(key))
        if canonical is None or value is None:
            continue
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        values[canonical] = values.get(canonical, 0) + amount

    if values and "total_tokens" not in values:
        values["total_tokens"] = values.get("prompt_tokens", 0) + values.get("completion_tokens", 0)
    return values


def _cache_summary(cached: int, missed: int) -> dict[str, Any]:
    """Cache counters plus the hit rate, omitted when no call reported them."""

    values: dict[str, Any] = {
        "cached_tokens": cached,
        "cache_miss_tokens": missed,
    }
    denominator = cached + missed
    if denominator > 0:
        values["cache_hit_rate"] = round(cached / denominator, 4)
    return values


class _Bucket:
    __slots__ = (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "cache_miss_tokens",
        "cache_calls",
        "calls",
        "unknown_calls",
    )

    def __init__(self) -> None:
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.cached_tokens = 0
        self.cache_miss_tokens = 0
        self.cache_calls = 0
        self.calls = 0
        self.unknown_calls = 0

    def add(self, usage: Mapping[str, int]) -> None:
        self.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        self.completion_tokens += int(usage.get("completion_tokens") or 0)
        self.total_tokens += int(usage.get("total_tokens") or 0)
        if "cached_tokens" in usage or "cache_miss_tokens" in usage:
            self.cache_calls += 1
            self.cached_tokens += int(usage.get("cached_tokens") or 0)
            self.cache_miss_tokens += int(usage.get("cache_miss_tokens") or 0)

    def as_dict(self) -> dict[str, int]:
        values: dict[str, int] = {
            "input_tokens": self.prompt_tokens,
            "output_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
            "unknown_calls": self.unknown_calls,
        }
        if self.cache_calls:
            values["cached_tokens"] = self.cached_tokens
            values["cache_miss_tokens"] = self.cache_miss_tokens
        return values


class RunUsageMeter:
    """Thread-safe usage accumulator for one run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets = {source: _Bucket() for source in SOURCES}
        self._pending_requests = 0
        self._aggregate = False
        self._transport = "local"
        self._unmetered: list[str] = []
        self._warnings: list[str] = []

    def set_transport(self, transport: str) -> None:
        with self._lock:
            self._transport = transport

    def mark_unmetered(self, source: str) -> None:
        with self._lock:
            if source not in self._unmetered:
                self._unmetered.append(source)

    def record(
        self,
        source: str,
        usage: Any,
        *,
        calls: int = 1,
        unknown_calls: int | None = None,
    ) -> bool:
        """Record one or more calls. Empty usage means the usage was not available."""

        normalized = normalize_usage(usage)
        calls = max(0, int(calls))
        with self._lock:
            if source == "agent_loop" and self._pending_requests:
                # A response settles the request marker emitted before the call.
                self._pending_requests = max(0, self._pending_requests - max(1, calls))
            bucket = self._bucket(source)
            bucket.calls += calls
            if normalized:
                bucket.add(normalized)
                bucket.unknown_calls += max(0, int(unknown_calls or 0))
            else:
                # No usage at all: every call in this update is unknown.
                bucket.unknown_calls += max(calls, max(0, int(unknown_calls or 0)))
        return bool(normalized)

    def record_request(self, source: str = "agent_loop") -> None:
        """Mark a model call that has not produced a response yet."""

        if source != "agent_loop":
            return
        with self._lock:
            self._pending_requests += 1

    def record_response(self, source: str, usage: Any) -> None:
        normalized = normalize_usage(usage)
        with self._lock:
            if source == "agent_loop" and self._pending_requests > 0:
                self._pending_requests -= 1
            bucket = self._bucket(source)
            bucket.calls += 1
            if normalized:
                bucket.add(normalized)
            else:
                bucket.unknown_calls += 1

    def record_aggregate(self, usage: Any) -> bool:
        """Use a transport-level aggregate only when no per-call data exists."""

        normalized = normalize_usage(usage)
        if not normalized:
            return False
        with self._lock:
            if self._aggregate or self._buckets["agent_loop"].calls or self._pending_requests:
                return False
            bucket = self._buckets["agent_loop"]
            bucket.calls = 1
            bucket.add(normalized)
            self._aggregate = True
        return True

    @property
    def agent_loop_calls(self) -> int:
        with self._lock:
            return self._buckets["agent_loop"].calls

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._pending_requests:
                bucket = self._buckets["agent_loop"]
                bucket.calls += self._pending_requests
                bucket.unknown_calls += self._pending_requests
                self._pending_requests = 0

            sources = {source: bucket.as_dict() for source, bucket in self._buckets.items()}
            model_calls = sum(bucket["calls"] for bucket in sources.values())
            unknown_calls = sum(bucket["unknown_calls"] for bucket in sources.values())
            prompt = sum(bucket["input_tokens"] for bucket in sources.values())
            completion = sum(bucket["output_tokens"] for bucket in sources.values())
            total = sum(bucket["total_tokens"] for bucket in sources.values())
            known = prompt > 0 or completion > 0 or total > 0

            if model_calls == 0:
                coverage = "none"
            elif self._aggregate:
                coverage = "aggregate"
            elif unknown_calls == 0:
                coverage = "complete"
            elif known:
                coverage = "partial"
            else:
                coverage = "unknown"
            if self._unmetered and coverage in {"complete", "aggregate"}:
                coverage = "partial"

            snapshot: dict[str, Any] = {
                "coverage": coverage,
                "transport": self._transport,
                "input_tokens": prompt if known else None,
                "output_tokens": completion if known else None,
                "total_tokens": total if known else None,
                "model_calls": model_calls,
                "unknown_calls": unknown_calls,
                "sources": sources,
            }
            cache_buckets = [bucket for bucket in self._buckets.values() if bucket.cache_calls]
            if cache_buckets:
                snapshot.update(
                    _cache_summary(
                        sum(bucket.cached_tokens for bucket in cache_buckets),
                        sum(bucket.cache_miss_tokens for bucket in cache_buckets),
                    )
                )
            if self._unmetered:
                snapshot["unmetered_sources"] = list(self._unmetered)
            if self._warnings:
                snapshot["warnings"] = list(self._warnings)
            return snapshot

    def _bucket(self, source: str) -> _Bucket:
        bucket = self._buckets.get(source)
        if bucket is None:
            bucket = _Bucket()
            self._buckets[source] = bucket
        return bucket


@contextmanager
def meter_context(meter: RunUsageMeter | None) -> Iterator[None]:
    """Bind ``meter`` for memory calls made in this context."""

    token = CURRENT_METER.set(meter)
    try:
        yield
    finally:
        CURRENT_METER.reset(token)


def usage_snapshot_from_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Recover a usage snapshot for a run finalized by another process."""

    usage = metadata.get("usage") if isinstance(metadata, Mapping) else None
    if isinstance(usage, Mapping) and isinstance(usage.get("coverage"), str):
        return dict(usage)

    normalized = normalize_usage(usage)
    if not normalized:
        return {
            "coverage": "unavailable",
            "model_calls": 0,
            "unknown_calls": 0,
            "sources": {},
        }
    prompt = normalized.get("prompt_tokens", 0)
    completion = normalized.get("completion_tokens", 0)
    total = normalized.get("total_tokens", prompt + completion)
    source: dict[str, int] = {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": total,
        "calls": 1,
        "unknown_calls": 0,
    }
    snapshot: dict[str, Any] = {
        "coverage": "aggregate",
        "transport": "legacy",
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": total,
        "model_calls": 1,
        "unknown_calls": 0,
        "sources": {"agent_loop": source},
    }
    if "cached_tokens" in normalized or "cache_miss_tokens" in normalized:
        cached = normalized.get("cached_tokens", 0)
        missed = normalized.get("cache_miss_tokens", 0)
        source.update(cached_tokens=cached, cache_miss_tokens=missed)
        snapshot.update(_cache_summary(cached, missed))
    return snapshot
