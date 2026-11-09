"""Human approval for skill script execution.

Script execution is a write-class action: the default policy asks the user
before the bundle is even built. When the run has no interactive channel, the
call fails closed and nothing is executed.
"""

from __future__ import annotations

import logging
import threading

from app.agent.interactions import RunInteraction
from app.agent.skills.config import SkillsConfig
from app.agent.skills.models import SkillRecord

logger = logging.getLogger(__name__)

_MAX_ARGS_PREVIEW = 500


def requires_script_approval(config: SkillsConfig, skill: SkillRecord) -> bool:
    """Return whether this skill must ask before running a script.

    ``auto`` skips the prompt only for operator-installed global skills; user
    and workspace skills always ask.
    """

    if config.script_approval == "auto" and skill.source == "global":
        return False
    return True


def request_script_approval(
    interaction: RunInteraction | None,
    skill: SkillRecord,
    script: str,
    args: str,
    preview: str,
    *,
    cancel_event: threading.Event | None,
    timeout_seconds: float | None = None,
) -> tuple[bool, str]:
    """Return ``(approved, denial_reason)``; never crashes the run."""

    if cancel_event is not None and cancel_event.is_set():
        return False, "Skill script execution was cancelled before approval."
    if interaction is None or getattr(interaction, "closed", False):
        return (
            False,
            f"Skill script '{skill.name}/{script}' requires user approval, "
            "but this run has no interactive approval channel.",
        )

    args_text = str(args or "")
    if len(args_text) > _MAX_ARGS_PREVIEW:
        args_text = args_text[:_MAX_ARGS_PREVIEW] + "..."
    question = (
        f"Allow running skill script '{skill.name}/{script}'?\n\n"
        f"Arguments: {args_text or '(none)'}\n\n"
        f"Script preview:\n{preview or '(empty)'}\n\n"
        "The script runs in the sandboxed executor with no external network."
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
        logger.warning("Skill script approval prompt failed: %s", type(exc).__name__)
        return False, "Skill script approval could not be requested."

    if "error" in answer:
        return False, f"Skill script was not approved ({answer.get('error')})."
    text = str(answer.get("answer") or "").strip().lower()
    if text.startswith(("allow", "yes")):
        return True, ""
    return False, "Skill script was not approved by the user."
