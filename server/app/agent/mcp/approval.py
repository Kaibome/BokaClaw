"""Annotation-based approval for MCP tool calls."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from app.agent.interactions import RunInteraction
from app.agent.mcp.client import MCPToolInfo
from app.agent.mcp.config import MCPServerConfig
from app.agent.mcp.security import sanitize_description

logger = logging.getLogger(__name__)

_MAX_ARGUMENT_CHARS = 1_000


def requires_approval(server: MCPServerConfig, info: MCPToolInfo) -> bool:
    if server.approval == "never":
        return False
    if server.approval == "always":
        return True
    return not (info.read_only and not info.destructive)


def _argument_preview(arguments: dict[str, Any]) -> str:
    try:
        text = json.dumps(arguments, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(arguments)
    if len(text) > _MAX_ARGUMENT_CHARS:
        text = text[:_MAX_ARGUMENT_CHARS] + "..."
    return text


def request_approval(
    interaction: RunInteraction | None,
    server: MCPServerConfig,
    info: MCPToolInfo,
    arguments: dict[str, Any],
    *,
    cancel_event: threading.Event | None,
    timeout_seconds: float | None,
) -> tuple[bool, str]:
    """Return ``(approved, denial_reason)``; never lets an approval crash the run."""

    if not requires_approval(server, info):
        return True, ""
    if cancel_event is not None and cancel_event.is_set():
        return False, "MCP tool call was cancelled before approval."
    if interaction is None or getattr(interaction, "closed", False):
        return (
            False,
            f"MCP tool '{server.name}/{info.name}' requires user approval, "
            "but this run has no interactive approval channel.",
        )

    description = sanitize_description(info.description, limit=500)
    question = (
        f"Allow MCP tool '{server.name}/{info.name}'?\n\n"
        f"{description or 'No description provided.'}\n\n"
        f"Arguments: {_argument_preview(arguments)}"
    )
    try:
        answer = interaction.ask(
            question=question,
            options=["Allow", "Deny"],
            allow_free_text=False,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
    except Exception as exc:  # noqa: BLE001 - approval must never crash the run
        logger.warning("MCP approval prompt failed: %s", type(exc).__name__)
        return False, "MCP tool approval could not be requested."

    if "error" in answer:
        return False, f"MCP tool was not approved ({answer.get('error')})."
    text = str(answer.get("answer") or "").strip().lower()
    if text.startswith(("allow", "yes")):
        return True, ""
    return False, "MCP tool was not approved by the user."
