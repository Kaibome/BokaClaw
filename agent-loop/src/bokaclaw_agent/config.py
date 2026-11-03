"""Runtime configuration models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelConfig(BaseModel):
    """Settings for the built-in OpenAI-compatible provider."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="Model name, for example ``gpt-4o-mini`` or ``deepseek-chat``.")
    api_key: str | None = Field(default=None, description="Defaults to ``OPENAI_API_KEY``.")
    base_url: str | None = Field(default=None, description="Defaults to ``OPENAI_BASE_URL`` or OpenAI's API.")
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    extra_body: dict[str, Any] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    """Behaviour of one agent runtime."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="agent", min_length=1)
    system_prompt: str | None = None
    max_iterations: int = Field(default=10, ge=1)
    tool_choice: str | dict[str, Any] = "auto"
    tool_error_mode: Literal["return", "raise"] = "return"
    max_history_messages: int | None = Field(default=None, ge=1)
    model: ModelConfig | None = Field(
        default=None,
        description="Used to build the default provider; ignored when a provider is injected.",
    )

    @field_validator("tool_choice")
    @classmethod
    def _validate_tool_choice(cls, value: str | dict[str, Any]) -> str | dict[str, Any]:
        if isinstance(value, str) and value not in {"auto", "none", "required"}:
            raise ValueError("tool_choice must be 'auto', 'none', 'required', or a dict")
        return value
