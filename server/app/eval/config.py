"""Environment configuration shared by the API and the eval worker.

This module must stay importable in the server process: it only uses the
standard library and never imports deepeval.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class EvalConfig:
    enabled: bool
    worker_poll_seconds: float
    heartbeat_seconds: int
    lease_seconds: int
    max_attempts: int
    max_run_seconds: int
    retention_days: int
    judge_enabled: bool
    judge_provider: str
    judge_model: str
    judge_api_key: str
    judge_max_chars: int

    @property
    def judge_available(self) -> bool:
        return self.judge_enabled and bool(self.judge_api_key)


def load_eval_config() -> EvalConfig:
    return EvalConfig(
        enabled=_env_bool("EVAL_ENABLED", True),
        worker_poll_seconds=_env_float("EVAL_WORKER_POLL_SECONDS", 2.0),
        heartbeat_seconds=max(1, _env_int("EVAL_HEARTBEAT_SECONDS", 10)),
        lease_seconds=max(5, _env_int("EVAL_LEASE_SECONDS", 60)),
        max_attempts=max(1, _env_int("EVAL_MAX_ATTEMPTS", 3)),
        max_run_seconds=max(30, _env_int("EVAL_MAX_RUN_SECONDS", 900)),
        retention_days=max(1, _env_int("EVAL_RETENTION_DAYS", 30)),
        judge_enabled=_env_bool("EVAL_JUDGE_ENABLED", False),
        judge_provider=os.environ.get("EVAL_JUDGE_PROVIDER", "deepseek").strip().lower(),
        judge_model=os.environ.get("EVAL_JUDGE_MODEL", "deepseek-chat").strip(),
        judge_api_key=os.environ.get("EVAL_JUDGE_API_KEY", "").strip(),
        judge_max_chars=max(1000, _env_int("EVAL_JUDGE_MAX_CHARS", 20000)),
    )


def apply_judge_env(config: EvalConfig) -> None:
    """Map ``EVAL_JUDGE_*`` onto deepeval's provider-specific variables.

    deepeval reads these at import time, so this must run before anything
    imports ``deepeval.config.settings``.
    """
    if not config.judge_available:
        return
    if config.judge_provider == "deepseek":
        os.environ["USE_DEEPSEEK_MODEL"] = "true"
        os.environ["DEEPSEEK_API_KEY"] = config.judge_api_key
        os.environ["DEEPSEEK_MODEL_NAME"] = config.judge_model
        return
    if config.judge_provider == "openai":
        os.environ.pop("USE_DEEPSEEK_MODEL", None)
        os.environ["OPENAI_API_KEY"] = config.judge_api_key
        os.environ["OPENAI_MODEL_NAME"] = config.judge_model
        return
    raise ValueError(
        f"Unsupported EVAL_JUDGE_PROVIDER {config.judge_provider!r}; use 'deepseek' or 'openai'."
    )
