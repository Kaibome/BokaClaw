"""Client for the out-of-process agent shell executor.

``Bash``, ``PowerShell`` and ``Monitor`` are the only tools that can touch
anything outside ``AGENT_WORKSPACE_ROOT``, so the server can delegate them to a
separate container (``server/app/agent/executor_app.py``). That container mounts the
workspace read-write at the same path, runs as a non-root user with a read-only
root filesystem, and is attached only to an internal Docker network, so it has
no route to the internet or to the other containers. ``/app`` (the BokaClaw
source baked into the image) stays readable but immutable there.

Set ``AGENT_EXECUTOR_URL`` to the executor base URL to enable delegation. When
it is empty the shell tools keep running in the server process (unsandboxed).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

from bokaclaw_agent import TransientToolError

DEFAULT_TIMEOUT_SECONDS = 120.0

# A retried call may reach an executor that was restarting, so transport
# failures and gateway errors are worth another attempt.
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class ExecutorConfig:
    """Where the shell executor lives and how to authenticate to it."""

    url: str
    token: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT_SECONDS


def load_executor_config() -> Optional[ExecutorConfig]:
    """Return executor settings, or ``None`` when shell commands stay in-process."""
    url = os.environ.get("AGENT_EXECUTOR_URL", "").strip()
    if not url:
        return None
    token = os.environ.get("AGENT_EXECUTOR_TOKEN", "").strip() or None
    timeout = max(1.0, _env_float("AGENT_EXECUTOR_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    return ExecutorConfig(url=url.rstrip("/"), token=token, timeout=timeout)


def _error_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        return str(body.get("detail") or body)[:500]
    return str(body)[:500]


def _post(config: ExecutorConfig, path: str, payload: Dict[str, Any], *, timeout: float) -> Dict[str, Any]:
    headers = {"Accept": "application/json"}
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{config.url}{path}", json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise TransientToolError(f"Shell executor timed out after {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise TransientToolError(f"Shell executor is unreachable: {exc}") from exc

    if response.status_code in _TRANSIENT_STATUS:
        retry_after = None
        header = response.headers.get("retry-after")
        if header:
            try:
                retry_after = max(0.0, float(header.strip()))
            except ValueError:
                retry_after = None
        raise TransientToolError(
            f"Shell executor is temporarily unavailable ({response.status_code}): {_error_detail(response)}",
            retry_after=retry_after,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Shell executor rejected the request ({response.status_code}): {_error_detail(response)}")
    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError("Shell executor returned invalid JSON") from exc


def run_command(
    config: ExecutorConfig,
    command: str,
    description: str = "",
    timeout_seconds: float = 120,
    *,
    shell: str = "bash",
    idempotency_key: str | None = None,
) -> Dict[str, Any]:
    """Run one command in the executor container and return its result.

    ``idempotency_key`` is stable across retries of one logical tool call, so
    the executor can collapse a duplicate request into the first response.
    """
    request_timeout = max(config.timeout, float(timeout_seconds) + 15.0)
    payload = {
        "command": command,
        "description": description,
        "timeout_seconds": int(max(1.0, float(timeout_seconds))),
        "shell": shell,
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return _post(config, "/exec", payload, timeout=request_timeout)


def run_skill(
    config: ExecutorConfig,
    *,
    skill: str,
    entrypoint: str,
    args: str,
    files: List[Dict[str, str]],
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Run one skill script inside the sandboxed executor.

    ``files`` is a bounded bundle of ``{"path": ..., "content_b64": ...}``
    entries built by the server. The executor unpacks it into a temporary
    directory, runs the entry point, and removes the directory afterwards.
    """

    payload = {
        "skill": skill,
        "entrypoint": entrypoint,
        "args": args,
        "files": files,
        "timeout_seconds": int(timeout_seconds),
    }
    request_timeout = max(config.timeout, float(timeout_seconds) + 30.0)
    return _post(config, "/skills/run", payload, timeout=request_timeout)


def monitor_action(
    config: ExecutorConfig,
    action: str,
    command: str = "",
    monitor_id: str = "",
    wait_seconds: float = 1.0,
    pattern: str = "",
    *,
    idempotency_key: str | None = None,
) -> Dict[str, Any]:
    """Start, poll, or stop a long-lived command in the executor container.

    ``idempotency_key`` matters for ``action="start"``: without it a retried
    start launches a second background command.
    """
    request_timeout = max(config.timeout, float(wait_seconds) + 15.0)
    payload = {
        "action": action,
        "command": command,
        "monitor_id": monitor_id,
        "wait_seconds": float(wait_seconds),
        "pattern": pattern,
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return _post(config, "/monitor", payload, timeout=request_timeout)
