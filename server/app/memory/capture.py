"""Post-turn episodic capture: filter noise, redact secrets, append one entry."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

from .grammar import MemoryEntry, stable_id

logger = logging.getLogger(__name__)

NON_INTERACTIVE_TRIGGERS = {"cron", "heartbeat", "automation", "schedule"}
_TOOL_TRUST_NAMES = {"websearch", "webfetch", "bash", "powershell", "execute_command", "execute_monitor"}

_NOISE_MESSAGE_PATTERNS = [
    re.compile(r"^(HEARTBEAT_OK|NO_REPLY)$", re.IGNORECASE),
    re.compile(r"^(ok|yes|no|sir|sure|thanks|done|good|nice|cool|got it|it's on|continue|alright|okay|yep|nope|hm+|hmm)$", re.IGNORECASE),
    re.compile(r"^[\s]*[\[{][\s\S]*[\]}][\s]*$"),
    re.compile(r"^(Error|Warning|Info|Debug):", re.IGNORECASE),
    re.compile(r"^(Loading|Loaded|Fetching|Fetched|Processing|Processed)\b", re.IGNORECASE),
    re.compile(r"^(SUCCESS|FAILURE|PENDING|COMPLETED|FAILED)$", re.IGNORECASE),
    re.compile(r"^(Calling|Called|Invoking|Invoked|Executing|Executed)\s+(function|tool|method)", re.IGNORECASE),
    re.compile(r"^Tool (call|result|output):", re.IGNORECASE),
    re.compile(r"^[\U0001F300-\U0001FAFF☀-➿\s]{1,5}$"),
]

_NOISE_CONTENT_PATTERNS = [
    re.compile(r"Conversation info \(untrusted metadata\):\s*```json[\s\S]*?```", re.IGNORECASE),
    re.compile(r"Sender\s*\(untrusted metadata\):\s*```json[\s\S]*?```", re.IGNORECASE),
    re.compile(r"\[media attached:[^\]]*\]", re.IGNORECASE),
    re.compile(r"<recalled-memories>[\s\S]*?</recalled-memories>", re.IGNORECASE),
    re.compile(r"```(?:json|text|output|result|log)[\s\S]*?```", re.IGNORECASE),
]

_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:authorization\s*[:=]\s*bearer\s+))\S+"),
    re.compile(r"(?i)((?:api[_-]?key|secret[_-]?access[_-]?key|session[_-]?token)\s*[:=]\s*)\S+"),
    re.compile(r"(?i)((?:(?:access|refresh)[_-]?token|password|credential)s?\s*[:=]\s*)\S+"),
    re.compile(r"\b(sk|m0|mem0_sk|psk-)[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b(ASIA|AKIA)[A-Z0-9]{12,}\b"),
    re.compile(r"\b(ghp_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
]

_GENERIC_ASSISTANT_PATTERNS = [
    re.compile(r"^(I see you've shared|Thanks for sharing|Got it[,.!]|How can I help|Let me know|I'll help|I'll assist|Sure[,.!]|Understood[,.!]|That's noted)", re.IGNORECASE),
]


@dataclass
class CaptureReport:
    action: str = "skipped"
    memory_id: str | None = None
    path: str | None = None
    chars: int = 0
    entries: int = 0
    trust: str = "owner"
    reason: str | None = None
    error: str | None = None


def redact_secrets(text: str) -> str:
    value = text or ""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda match: match.group(1) + "[redacted]" if match.groups() else "[redacted]", value)
    return value


def strip_noise(text: str) -> str:
    value = text or ""
    for pattern in _NOISE_CONTENT_PATTERNS:
        value = pattern.sub("", value)
    value = redact_secrets(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def is_noise(text: str) -> bool:
    value = (text or "").strip()
    if not value:
        return True
    return any(pattern.match(value) for pattern in _NOISE_MESSAGE_PATTERNS)


def _is_generic_assistant(text: str) -> bool:
    value = text.strip()
    if len(value) > 300:
        return False
    return any(pattern.match(value) for pattern in _GENERIC_ASSISTANT_PATTERNS)


def _too_short(text: str) -> bool:
    """Require roughly 12 tokens; CJK is ~1 char/token, ASCII ~0.25."""

    value = (text or "").strip()
    if not value:
        return True
    ascii_chars = sum(1 for char in value if ord(char) < 128)
    cjk_chars = len(value) - ascii_chars
    return cjk_chars + ascii_chars * 0.25 < 12


def _trust_for(tool_entries: Iterable[Dict[str, Any]]) -> str:
    for entry in tool_entries or ():
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").replace("_", "").replace("-", "").lower()
        if name in _TOOL_TRUST_NAMES:
            return "tool"
    return "owner"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def capture_turn(
    service: Any,
    user_id: str,
    user_message: str,
    assistant_message: str,
    *,
    tool_entries: Iterable[Dict[str, Any]] = (),
    metadata: Dict[str, Any] | None = None,
    conversation_id: str | None = None,
    run_id: str | None = None,
    trigger: str | None = None,
    is_subagent: bool = False,
    used_memory_tool: bool = False,
    injected_memory: bool = False,
) -> CaptureReport:
    """Append one raw episode for a completed turn. Never raises."""

    settings = service.settings
    report = CaptureReport()
    if not settings.capture_enabled:
        report.reason = "disabled"
        return report
    if not user_id:
        report.reason = "no_user"
        return report
    if is_subagent:
        report.reason = "subagent"
        return report
    if trigger and str(trigger).lower() in NON_INTERACTIVE_TRIGGERS:
        report.reason = "non_interactive"
        return report
    if used_memory_tool:
        report.reason = "memory_tool_used"
        return report
    if injected_memory:
        report.reason = "injected_memory"
        return report

    user_text = strip_noise(user_message)
    assistant_text = strip_noise(assistant_message)
    if _too_short(user_text) or is_noise(user_text):
        report.reason = "short_or_noise"
        return report
    if assistant_text and _is_generic_assistant(assistant_text):
        report.reason = "generic_assistant"
        return report

    trust = _trust_for(tool_entries)
    text = f"**User:** {_clip(user_text, 2000)}"
    if assistant_text:
        text += f"\n**Assistant:** {_clip(assistant_text, 4000)}"
    now = datetime.now(timezone.utc)
    entry = MemoryEntry(
        id=stable_id("episode", text),
        kind="episode",
        content=text,
        importance=0.6,
        source="capture",
        trust=trust,
        created=now,
        updated=now,
        metadata={k: v for k, v in (metadata or {}).items() if v is not None},
        user_id=user_id,
        run_id=run_id,
        conversation_id=conversation_id,
    )
    report.chars = len(text)
    report.trust = trust
    try:
        existing = service.repository.get_entry(entry.id, show_expired=True)
        if existing is not None:
            report.action = "skipped"
            report.reason = "duplicate"
            report.memory_id = entry.id
            return report
        path = service.store.append_entry(user_id, entry)
        try:
            service.repository.upsert_entries([entry])
            service.repository.record_history(entry.id, user_id, "ADD", None, entry.content)
        except Exception as exc:
            if hasattr(service, "mark_dirty"):
                service.mark_dirty(user_id, exc)
            raise
        if hasattr(service, "refresh_file_state"):
            service.refresh_file_state(user_id, path)
        report.action = "appended"
        report.memory_id = entry.id
        report.path = str(path)
        report.entries = 1
        return report
    except Exception as exc:
        logger.warning("Episodic capture failed for %s", user_id, exc_info=True)
        report.action = "failed"
        report.error = f"{type(exc).__name__}: {exc}"
        return report
