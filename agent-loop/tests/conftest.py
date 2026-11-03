from __future__ import annotations

from typing import Any

import pytest

from bokaclaw_agent.types import ModelResponse


class FakeProvider:
    """Scripted provider used by unit tests; records every request."""

    def __init__(self, responses: list[ModelResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ) -> ModelResponse:
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": tools,
                "tool_choice": tool_choice,
                "kwargs": kwargs,
            }
        )
        if not self.responses:
            raise AssertionError("FakeProvider has no scripted responses left")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def provider_factory():
    return FakeProvider
