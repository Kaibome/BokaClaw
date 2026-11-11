"""Deferred tool catalog and keyword search for the ``ToolSearch`` tool.

Most tools are cheap to keep in the model's tool list, but MCP servers and
low-frequency Office edits add schemas the model rarely needs. They stay
registered so dispatch works, while ``ToolRegistry.specs()`` hides them until
``ToolSearch`` activates them for this run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

DEFERRED_OFFICE_TOOLS = frozenset(
    {
        "PptxUpdateSlide",
        "PptxAddSlide",
        "PptxDeleteSlide",
    }
)

CATALOG_MAX_CHARS = 6_000
CATALOG_DESCRIPTION_CHARS = 180
SEARCH_DESCRIPTION_CHARS = 400
MAX_SEARCH_RESULTS = 10

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class DeferredTool:
    name: str
    description: str
    group: str
    tool: Any


def is_deferred(name: str) -> bool:
    return name.startswith("mcp__") or name in DEFERRED_OFFICE_TOOLS


def _group_for(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        return parts[1] if len(parts) > 1 and parts[1] else "mcp"
    if name in DEFERRED_OFFICE_TOOLS:
        return "office"
    return "other"


def collect_deferred(tools: Iterable[Any]) -> dict[str, DeferredTool]:
    """Index the registered tools that should start hidden from the model."""

    deferred: dict[str, DeferredTool] = {}
    for tool in tools:
        name = getattr(tool, "name", "")
        if not isinstance(name, str) or not is_deferred(name):
            continue
        description = getattr(tool, "description", "")
        deferred[name] = DeferredTool(
            name=name,
            description=str(description or ""),
            group=_group_for(name),
            tool=tool,
        )
    return deferred


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN.findall(text.lower()) if len(token) > 1}


def search_deferred(
    deferred: Mapping[str, DeferredTool],
    query: str,
    *,
    limit: int = 5,
) -> list[DeferredTool]:
    """Rank deferred tools by name and description overlap with ``query``."""

    text = (query or "").strip().lower()
    if not text:
        return []
    bounded = max(1, min(int(limit), MAX_SEARCH_RESULTS))
    terms = _tokens(text)
    scored: list[tuple[int, DeferredTool]] = []
    for item in deferred.values():
        name = item.name.lower()
        description = item.description.lower()
        score = 0
        if text in name:
            score += 20
        for term in terms:
            if term in name:
                score += 8
            if term in description:
                score += 2
        if score:
            scored.append((score, item))
    scored.sort(key=lambda entry: (-entry[0], entry[1].name))
    return [item for _, item in scored[:bounded]]


def render_deferred_catalog(
    deferred: Mapping[str, DeferredTool],
    *,
    active_names: Iterable[str] = (),
    max_chars: int = CATALOG_MAX_CHARS,
) -> str | None:
    """Render the compact catalog injected before the current user message."""

    active = set(active_names)
    pending = [item for item in deferred.values() if item.name not in active]
    if not pending:
        return None

    limit = max(256, int(max_chars))
    header = (
        "<deferred-tools>\n"
        "These tools are registered but not loaded. Call ToolSearch(query) to load a matching "
        'tool before using it; ToolSearch("") lists this catalog again.\n'
    )
    footer = "\n</deferred-tools>"

    groups: dict[str, list[DeferredTool]] = {}
    for item in sorted(pending, key=lambda entry: (entry.group, entry.name)):
        groups.setdefault(item.group, []).append(item)

    body: list[str] = []
    for group, items in sorted(groups.items()):
        body.append(f"[{group}]")
        for item in items:
            body.append(f"- {item.name} — {_short(item.description, CATALOG_DESCRIPTION_CHARS)}")
        body.append("")

    text = header + "\n".join(body) + footer
    if len(text) <= limit:
        return text

    kept: list[str] = []
    used = len(header) + len(footer)
    for line in body:
        if used + len(line) + 1 > limit - 80:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append('Catalog truncated; call ToolSearch("") for the full list.')
    return header + "\n".join(kept) + footer


def tool_search_result(
    deferred: Mapping[str, DeferredTool],
    registry: Any,
    query: str,
    *,
    limit: int = 5,
) -> dict[str, Any]:
    """Return the ToolSearch payload and activate matched tools in ``registry``."""

    text = (query or "").strip()
    if not text:
        catalog = [
            {
                "name": item.name,
                "group": item.group,
                "description": _short(item.description, CATALOG_DESCRIPTION_CHARS),
                "loaded": bool(
                    registry is not None
                    and hasattr(registry, "active_names")
                    and item.name in registry.active_names
                ),
            }
            for item in sorted(deferred.values(), key=lambda entry: (entry.group, entry.name))
        ]
        return {"activated": [], "catalog": catalog}

    matches = search_deferred(deferred, text, limit=limit)
    names = [item.name for item in matches]
    if registry is not None and hasattr(registry, "activate"):
        registry.activate(names)

    tools: list[dict[str, Any]] = []
    for item in matches:
        spec = getattr(item.tool, "to_openai_spec", None)
        if callable(spec):
            function = spec().get("function") or {}
            tools.append(
                {
                    "name": function.get("name") or item.name,
                    "description": function.get("description") or _short(item.description, SEARCH_DESCRIPTION_CHARS),
                    "parameters": function.get("parameters") or {},
                }
            )
        else:
            tools.append({"name": item.name, "description": _short(item.description, SEARCH_DESCRIPTION_CHARS)})

    active = getattr(registry, "active_names", frozenset()) if registry is not None else frozenset()
    remaining = sum(1 for item in deferred.values() if item.name not in active)
    result: dict[str, Any] = {
        "activated": names,
        "tools": tools,
        "remaining": remaining,
    }
    if not matches:
        result["hint"] = 'No deferred tool matched; call ToolSearch("") to list every deferred tool.'
    return result
