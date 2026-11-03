"""Isolated HTTP service that runs agent shell commands.

This module is served in its own container (``uvicorn executor_app:app``) with a
read-only root filesystem, no route to the internet, dropped capabilities, and
only the agent workspace mounted read-write at ``AGENT_WORKSPACE_ROOT``.
``/app`` -- the BokaClaw source baked into the image -- is readable but
immutable there, so the agent can inspect the code without being able to change
it. The BokaClaw server reaches this service over the internal Docker network
``bokaclaw-exec``.

Every command runs with a sanitised environment, so the executor's own token
and the server's API keys are never exposed to the shell.
"""

from __future__ import annotations

import os
import secrets
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.agent.dev_tools import DevToolConfig, load_dev_tool_config, monitor_action, run_command


class ExecRequest(BaseModel):
    """One foreground command."""

    command: str = Field(min_length=1)
    description: str = ""
    timeout_seconds: int = Field(default=120, ge=1, le=3600)
    shell: Literal["bash", "powershell"] = "bash"


class MonitorRequest(BaseModel):
    """Start, poll, or stop a background command."""

    action: Literal["start", "poll", "stop"]
    command: str = ""
    monitor_id: str = ""
    wait_seconds: float = Field(default=1.0, ge=0, le=30)
    pattern: str = ""


def create_app(config: Optional[DevToolConfig] = None, token: Optional[str] = None) -> FastAPI:
    """Build the executor app for ``config``, requiring ``token`` when set."""
    cfg = config if config is not None else load_dev_tool_config()
    if cfg is None:
        raise RuntimeError("AGENT_DEV_TOOLS must be enabled in the executor container")

    expected_token = (token if token is not None else os.environ.get("AGENT_EXECUTOR_TOKEN", "")).strip()

    app = FastAPI(title="bokaclaw-executor", docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: Optional[str] = Header(default=None)) -> None:
        if not expected_token:
            return
        if not authorization or not secrets.compare_digest(authorization, f"Bearer {expected_token}"):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "workspace": str(cfg.workspace), "shell": cfg.allow_shell}

    @app.post("/exec", dependencies=[Depends(authorize)])
    def exec_command(payload: ExecRequest) -> dict:
        try:
            return run_command(
                cfg,
                payload.command,
                payload.description,
                payload.timeout_seconds,
                shell=payload.shell,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    @app.post("/monitor", dependencies=[Depends(authorize)])
    def monitor(payload: MonitorRequest) -> dict:
        try:
            return monitor_action(
                cfg,
                payload.action,
                payload.command,
                payload.monitor_id,
                payload.wait_seconds,
                payload.pattern,
            )
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return app


app = create_app()
