"""Detect and recover raw DSML tool-call markup in model output.

Some OpenAI-compatible providers occasionally put a tool call in the assistant
text using the model's native DSML tags instead of the structured
``tool_calls`` field::

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="Bash">
    <｜｜DSML｜｜ parameter name="command" string="true">ls -la</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

The runtime must never accept that text as a final answer. This module detects
the markup, strips it, and parses it back into :class:`ToolCall` objects so the
runtime can execute the intended call instead of silently returning raw markup.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any
from uuid import uuid4

from ..core.types import ToolCall

DSML_MARKER = "<｜｜DSML｜｜"

_OPEN = re.escape(DSML_MARKER)
_CLOSE = re.escape("</｜｜DSML｜｜")
_DSML_CALLS_BLOCK = re.compile(rf"{_OPEN}\s*calls\s*>.*?{_CLOSE}\s*calls\s*>", re.DOTALL)
_DSML_INVOKE = re.compile(
    rf"{_OPEN}\s*invoke\s+name=[\"'](?P<name>[^\"']+)[\"']\s*>(?P<body>.*?){_CLOSE}\s*invoke\s*>",
    re.DOTALL,
)
_DSML_PARAM = re.compile(
    rf"{_OPEN}\s*parameter\s+name=[\"'](?P<name>[^\"']+)[\"']"
    rf"(?:\s+string=[\"'](?P<string>true|false)[\"'])?\s*>(?P<value>.*?){_CLOSE}\s*parameter\s*>",
    re.DOTALL,
)


def contains_dsml(text: str | None) -> bool:
    """Return whether ``text`` contains a raw DSML tag."""

    return bool(text) and DSML_MARKER in text


def strip_dsml(text: str) -> str:
    """Remove DSML blocks from ``text`` while keeping any surrounding prose."""

    cleaned = _DSML_CALLS_BLOCK.sub("", text)
    if DSML_MARKER in cleaned:
        # A malformed or unclosed block: drop every line that still carries a
        # tag so no raw markup can reach the user.
        cleaned = "\n".join(line for line in cleaned.splitlines() if DSML_MARKER not in line)
    return cleaned.strip()


def parse_dsml_tool_calls(text: str | None) -> list[ToolCall]:
    """Parse DSML ``invoke`` blocks into tool calls.

    Parameters marked ``string="true"`` are passed through as text (with HTML
    entities decoded). Parameters marked ``string="false"`` are decoded as JSON
    when possible and otherwise kept as text. Unparseable markup yields ``[]``
    so the caller can reject the response instead of guessing.
    """

    if not text or DSML_MARKER not in text:
        return []

    calls: list[ToolCall] = []
    for match in _DSML_INVOKE.finditer(text):
        name = match.group("name").strip()
        if not name:
            continue
        arguments: dict[str, Any] = {}
        for param in _DSML_PARAM.finditer(match.group("body")):
            key = param.group("name").strip()
            if not key:
                continue
            raw = _trim_newlines(param.group("value"))
            if (param.group("string") or "true").lower() == "false":
                try:
                    arguments[key] = json.loads(raw)
                except (TypeError, ValueError):
                    arguments[key] = raw
            else:
                arguments[key] = html.unescape(raw)
        calls.append(
            ToolCall(
                id=f"call_dsml_{uuid4().hex}",
                name=name,
                arguments=arguments,
                raw_arguments=json.dumps(arguments, ensure_ascii=False),
            )
        )
    return calls


def _trim_newlines(value: str) -> str:
    """Drop one decorative newline on each side without touching the payload."""

    if value.startswith("\r\n"):
        value = value[2:]
    elif value.startswith("\n"):
        value = value[1:]
    if value.endswith("\r\n"):
        value = value[:-2]
    elif value.endswith("\n"):
        value = value[:-1]
    return value


__all__ = ["DSML_MARKER", "contains_dsml", "parse_dsml_tool_calls", "strip_dsml"]
