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

import base64
import binascii
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import List, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

from app.agent.dev_tools import (
    DevToolConfig,
    _truncate,
    command_env,
    load_dev_tool_config,
    monitor_action,
    run_command,
)

SKILL_INTERPRETERS = {
    ".sh": "bash",
    ".py": "python3",
    ".js": "node",
    ".mjs": "node",
}
MAX_SKILL_FILES = 200
MAX_SKILL_BUNDLE_BYTES = 5_000_000
MAX_SKILL_TIMEOUT_SECONDS = 600
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


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


class SkillBundleFile(BaseModel):
    """One file in a skill bundle, base64-encoded for JSON transport."""

    path: str = Field(min_length=1, max_length=512)
    content_b64: str = Field(default="", max_length=8_000_000)


class SkillRunRequest(BaseModel):
    """Run one script from a skill bundle in an isolated temporary directory."""

    skill: str = Field(min_length=1, max_length=64)
    entrypoint: str = Field(min_length=1, max_length=512)
    args: str = Field(default="", max_length=65_536)
    files: List[SkillBundleFile] = Field(min_length=1, max_length=MAX_SKILL_FILES)
    timeout_seconds: int = Field(default=120, ge=1, le=MAX_SKILL_TIMEOUT_SECONDS)


def _safe_bundle_path(raw: str) -> PurePosixPath:
    """Reject absolute, traversal, hidden, and non-POSIX bundle paths."""

    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid bundle path")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".."} for part in parts) or any(part.startswith(".") for part in parts):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid bundle path")
    return PurePosixPath(*parts)


def _validate_entrypoint(raw: str) -> PurePosixPath:
    path = _safe_bundle_path(raw)
    if len(path.parts) < 2 or path.parts[0] != "scripts":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="entrypoint must be under scripts/",
        )
    if Path(path.name).suffix.lower() not in SKILL_INTERPRETERS:
        allowed = ", ".join(sorted(SKILL_INTERPRETERS))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unsupported script type; use one of: {allowed}",
        )
    return path


def _decode_bundle(payload: SkillRunRequest, entrypoint: PurePosixPath) -> dict[PurePosixPath, bytes]:
    decoded: dict[PurePosixPath, bytes] = {}
    total_bytes = 0
    for item in payload.files:
        relative = _safe_bundle_path(item.path)
        if relative in decoded:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"duplicate bundle path: {item.path}",
            )
        try:
            data = base64.b64decode(item.content_b64.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"invalid base64 content for {item.path}",
            ) from None
        total_bytes += len(data)
        if total_bytes > MAX_SKILL_BUNDLE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"skill bundle exceeds {MAX_SKILL_BUNDLE_BYTES} bytes",
            )
        decoded[relative] = data

    if entrypoint not in decoded:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="entrypoint is missing from the bundle",
        )
    return decoded


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        return
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


def run_skill_bundle(config: DevToolConfig, payload: SkillRunRequest) -> dict:
    """Unpack, execute, and clean up one skill script bundle."""

    if not _SKILL_NAME_RE.match(payload.skill):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid skill name")
    entrypoint = _validate_entrypoint(payload.entrypoint)
    if "\x00" in payload.args:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="args must not contain a NUL byte")
    try:
        argv_args = shlex.split(payload.args, posix=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"args is not valid shell-like quoting: {exc}",
        ) from exc

    decoded = _decode_bundle(payload, entrypoint)
    root = Path(tempfile.mkdtemp(prefix="skill-run-", dir="/tmp")).resolve()
    try:
        for relative, data in decoded.items():
            target = (root / Path(*relative.parts)).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"bundle path escapes the temporary directory: {relative.as_posix()}",
                ) from None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o755 if relative.parts[0] == "scripts" else 0o644)

        entry_path = (root / Path(*entrypoint.parts)).resolve()
        if not entry_path.is_file():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="entrypoint was not written to the temporary directory",
            )
        interpreter = SKILL_INTERPRETERS[Path(entrypoint.name).suffix.lower()]
        argv = [interpreter, str(entry_path), *argv_args]
        environment = command_env(config)
        environment["HOME"] = str(root)
        environment["PWD"] = str(root)
        # czsc 0.10.x binds the Rust CZSC by default, whose constructor does not
        # accept the ``get_signals=`` keyword used by classic czsc skills. The
        # compatibility switch forces the pure-Python implementation and only
        # affects this child process.
        environment["CZSC_USE_PYTHON"] = "1"

        started = time.monotonic()
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"could not start script interpreter: {type(exc).__name__}",
            ) from exc

        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=payload.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate(process)
            stdout, stderr = process.communicate()

        duration_ms = int((time.monotonic() - started) * 1000)
        stdout, stdout_truncated = _truncate(stdout or "", config.max_output_bytes)
        stderr, stderr_truncated = _truncate(stderr or "", config.max_output_bytes)
        return {
            "skill": payload.skill,
            "entrypoint": entrypoint.as_posix(),
            "exit_code": process.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": timed_out,
            "truncated": stdout_truncated or stderr_truncated,
            "duration_ms": duration_ms,
            "timeout_seconds": payload.timeout_seconds,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


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

    @app.post("/skills/run", dependencies=[Depends(authorize)])
    def run_skill(payload: SkillRunRequest) -> dict:
        return run_skill_bundle(cfg, payload)

    return app


app = create_app()
