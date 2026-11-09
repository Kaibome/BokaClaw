"""Convert MCP call results into model-visible text."""

from __future__ import annotations

import json
from typing import Any

from app.agent.mcp.client import MCPCallOutcome, MCPContent, MCPToolInfo
from app.agent.mcp.config import MCPServerConfig
from app.agent.mcp.security import truncate_head_tail, wrap_untrusted

_STRUCTURED_MAX_CHARS = 8_000


def _render_content(content: MCPContent) -> str:
    if content.kind == "text":
        return content.text or ""
    if content.kind == "image":
        return (
            f"[image omitted: {content.mime_type or 'unknown type'}, "
            f"{content.data_chars} base64 chars; this runtime is text-only]"
        )
    if content.kind == "audio":
        return (
            f"[audio omitted: {content.mime_type or 'unknown type'}, "
            f"{content.data_chars} base64 chars; this runtime is text-only]"
        )
    if content.kind == "resource_text":
        return f"[resource {content.uri or 'unknown'}]\n{content.text or ''}"
    if content.kind == "resource_blob":
        return (
            f"[binary resource {content.uri or 'unknown'}: "
            f"{content.mime_type or 'unknown type'}, {content.data_chars} base64 chars]"
        )
    if content.kind == "resource_link":
        return f"[resource link {content.uri or 'unknown'}]"
    return content.text or f"[unsupported content: {content.kind}]"


def _render_structured(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > _STRUCTURED_MAX_CHARS:
        text = text[:_STRUCTURED_MAX_CHARS] + "... [structuredContent truncated]"
    return f"[structuredContent]\n{text}"


def format_outcome(
    server: MCPServerConfig,
    info: MCPToolInfo,
    outcome: MCPCallOutcome,
    *,
    max_chars: int,
) -> tuple[str, bool, dict[str, Any]]:
    """Return ``(content, is_error, metadata)`` for a ``ToolResult``."""

    parts = [_render_content(item) for item in outcome.contents]
    if outcome.structured is not None:
        parts.append(_render_structured(outcome.structured))
    body = "\n\n".join(part for part in parts if part)
    if not body:
        body = "[empty result]"
    if outcome.is_error:
        body = f"MCP tool reported an error.\n\n{body}"

    body, _ = truncate_head_tail(body, max(256, max_chars - 512))
    wrapped = wrap_untrusted(server.name, info.name, body)
    metadata = {"mcp_server": server.name, "mcp_tool": info.name}
    return wrapped, outcome.is_error, metadata
