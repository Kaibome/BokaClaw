"""The ``ask_user`` tool: pause the run until the user answers."""

from __future__ import annotations

import threading
from typing import Any, Optional

from .interactions import MAX_TIMEOUT_SECONDS, MIN_TIMEOUT_SECONDS, RunInteraction

MAX_QUESTION_CHARS = 2_000
MAX_OPTIONS = 8
MAX_OPTION_CHARS = 200

USER_INPUT_GUIDANCE = (
    "You can ask the user for information that no tool can discover and that you need to "
    "finish the task. Call ask_user only when the answer changes what you do next. Do not ask "
    "for confirmation, for preferences the user already stated, or for something you can look "
    "up. Ask one focused question at a time. Offer 2-4 concrete options when they cover the "
    "likely answers; set allow_free_text=false only when the options are exhaustive. While the "
    "question is pending the user is answering: do not claim the task is complete."
)


def build_user_input_tool(
    sdk: Any,
    interaction: RunInteraction,
    *,
    cancel_event: threading.Event | None = None,
    default_timeout_seconds: float = 240.0,
) -> Any:
    """Build the ``ask_user`` tool bound to one run's interaction broker."""

    def ask_user(
        question: str,
        options: Optional[list[str]] = None,
        allow_free_text: bool = True,
        timeout_seconds: float = 0,
    ) -> dict[str, Any]:
        """Ask the user for information required to continue.

        Args:
            question: One focused question, at most 2000 characters.
            options: Optional concrete choices; at most 8, each at most 200 characters.
            allow_free_text: Whether the user may answer in their own words instead of picking an option.
            timeout_seconds: How long to wait; 0 uses the server default.
        """

        text = (question or "").strip()
        if not text:
            return {"error": "question must not be empty"}
        if len(text) > MAX_QUESTION_CHARS:
            return {"error": f"question must be at most {MAX_QUESTION_CHARS} characters"}

        choices = [str(item).strip() for item in (options or ()) if str(item).strip()]
        if len(choices) > MAX_OPTIONS:
            return {"error": f"at most {MAX_OPTIONS} options are allowed"}
        if any(len(item) > MAX_OPTION_CHARS for item in choices):
            return {"error": f"each option must be at most {MAX_OPTION_CHARS} characters"}
        if not choices and not allow_free_text:
            return {"error": "allow_free_text is false but no options were provided"}

        try:
            timeout = float(timeout_seconds or 0)
        except (TypeError, ValueError):
            timeout = 0
        if timeout <= 0:
            timeout = default_timeout_seconds
        timeout = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, timeout))

        return interaction.ask(
            question=text,
            options=choices or None,
            allow_free_text=allow_free_text,
            timeout_seconds=timeout,
            cancel_event=cancel_event,
        )

    return sdk.tool(ask_user, name="ask_user", effect="write")
