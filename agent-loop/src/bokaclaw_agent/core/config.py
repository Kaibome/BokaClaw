"""Runtime configuration models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    final_answer_on_max_iterations: bool = Field(
        default=True,
        description=(
            "When the step budget runs out while the model is still calling tools, make one "
            "final tool-free request so it answers from what it already gathered instead of "
            "failing the run."
        ),
    )
    tool_choice: str | dict[str, Any] = "auto"
    tool_error_mode: Literal["return", "raise"] = "return"
    max_history_messages: int | None = Field(default=None, ge=1)
    context_max_tokens: int | None = Field(
        default=None,
        ge=1024,
        description=(
            "Approximate token budget for the model view. When exceeded, older messages are "
            "summarized or dropped. Disabled when None."
        ),
    )
    context_keep_recent_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Recent tokens always kept by context compression; defaults to half the budget.",
    )
    context_compression: Literal["summary", "truncate"] = Field(
        default="summary",
        description="How to compress over-budget context: summarize with the provider or only drop.",
    )
    tool_result_max_chars: int | None = Field(
        default=None,
        ge=256,
        description=(
            "Maximum characters of a tool result in the model view. Full content is still "
            "persisted and returned in RunResult.tool_results. Disabled when None."
        ),
    )
    fact_ledger: bool = Field(
        default=False,
        description=(
            "Keep a compact run-scoped ledger of tool calls and findings in the model view. "
            "It is appended after context compression, so early facts survive summarization; "
            "nothing extra is persisted."
        ),
    )
    fact_ledger_max_chars: int = Field(
        default=2_500,
        ge=256,
        description="Maximum characters of the fact ledger injected into the model view.",
    )
    step_budget_notice: bool = Field(
        default=False,
        description=(
            "Append the current step number and, near the end of the budget, a wrap-up "
            "directive to the model view. The notice is never persisted."
        ),
    )
    progress_guard: bool = Field(
        default=False,
        description=(
            "Detect tool calls that return no new evidence. After progress_nudge_after "
            "consecutive calls the model is told to change course; after "
            "progress_stop_after it is forced to answer. Neither notice is persisted."
        ),
    )
    progress_nudge_after: int = Field(
        default=3,
        ge=2,
        description="Consecutive no-progress tool calls before the model is nudged.",
    )
    progress_stop_after: int = Field(
        default=5,
        ge=2,
        description=(
            "Consecutive no-progress tool calls before the runtime forces a final answer "
            "with stop_reason 'no_progress'. Must be at least progress_nudge_after."
        ),
    )
    model: ModelConfig | None = Field(
        default=None,
        description="Used to build the default provider; ignored when a provider is injected.",
    )
    compression_model: ModelConfig | None = Field(
        default=None,
        description=(
            "Provider used for context-compression summaries. Defaults to the agent's own "
            "provider when None. Ignored unless context compression is enabled."
        ),
    )

    @field_validator("tool_choice")
    @classmethod
    def _validate_tool_choice(cls, value: str | dict[str, Any]) -> str | dict[str, Any]:
        if isinstance(value, str) and value not in {"auto", "none", "required"}:
            raise ValueError("tool_choice must be 'auto', 'none', 'required', or a dict")
        return value

    @model_validator(mode="after")
    def _validate_context_budget(self) -> "AgentConfig":
        budget = self.context_max_tokens
        keep_recent = self.context_keep_recent_tokens
        if budget is not None and keep_recent is not None and keep_recent >= budget - 256:
            raise ValueError("context_keep_recent_tokens must be at least 256 tokens below context_max_tokens")
        return self

    @model_validator(mode="after")
    def _validate_progress_thresholds(self) -> "AgentConfig":
        if self.progress_stop_after < self.progress_nudge_after:
            raise ValueError("progress_stop_after must be greater than or equal to progress_nudge_after")
        return self
