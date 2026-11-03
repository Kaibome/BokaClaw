"""OpenAI Chat Completions provider that preserves tool-call ids."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from openai import OpenAI

from ..config import ModelConfig
from ..exceptions import ConfigurationError, ModelError
from ..types import ModelResponse, ToolCall


class OpenAICompatibleProvider:
    """Call any OpenAI-compatible ``/chat/completions`` endpoint.

    Works with OpenAI, DeepSeek, Qwen/DashScope compatible mode, vLLM, Ollama's
    OpenAI endpoint, and similar services. The key behaviour for ReAct is that
    ``tool_call.id`` is preserved and ``function.arguments`` is parsed without
    discarding the raw string.
    """

    def __init__(self, config: ModelConfig, client: OpenAI | None = None) -> None:
        self.config = config
        if client is not None:
            self._client = client
            return

        api_key = config.api_key or os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ConfigurationError(
                "Model API key is missing. Set ModelConfig.api_key or the OPENAI_API_KEY environment variable."
            )

        base_url = config.base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=config.timeout,
            max_retries=config.max_retries,
            default_headers=config.extra_headers or None,
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ) -> ModelResponse:
        params: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
        }
        if self.config.temperature is not None:
            params["temperature"] = self.config.temperature
        if self.config.max_tokens is not None:
            params["max_tokens"] = self.config.max_tokens
        if tools:
            params["tools"] = tools
            if tool_choice is not None:
                params["tool_choice"] = tool_choice
        if self.config.extra_body:
            params["extra_body"] = self.config.extra_body
        params.update(kwargs)

        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001 - provider errors are normalized
            raise ModelError(f"Model request failed: {exc}") from exc

        return self._parse_response(response)

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None, str | None]:
        if isinstance(raw_arguments, dict):
            return dict(raw_arguments), json.dumps(raw_arguments, ensure_ascii=False), None

        raw = raw_arguments if isinstance(raw_arguments, str) else ""
        if not raw.strip():
            return {}, raw, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, raw, str(exc)
        if not isinstance(parsed, dict):
            return {}, raw, "tool arguments must be a JSON object"
        return parsed, raw, None

    @classmethod
    def _parse_tool_call(cls, raw_call: Any) -> ToolCall | None:
        function = getattr(raw_call, "function", None)
        if function is None and isinstance(raw_call, dict):
            function = raw_call.get("function")
        if function is None:
            return None

        if isinstance(function, dict):
            name = function.get("name")
            raw_arguments = function.get("arguments")
        else:
            name = getattr(function, "name", None)
            raw_arguments = getattr(function, "arguments", None)
        if not name:
            return None

        call_id = getattr(raw_call, "id", None)
        if call_id is None and isinstance(raw_call, dict):
            call_id = raw_call.get("id")
        arguments, raw, error = cls._parse_arguments(raw_arguments)

        return ToolCall(
            id=str(call_id) if call_id else f"call_{uuid4().hex}",
            name=str(name),
            arguments=arguments,
            raw_arguments=raw,
            arguments_error=error,
        )

    @classmethod
    def _parse_response(cls, response: Any) -> ModelResponse:
        try:
            choice = response.choices[0]
        except (AttributeError, IndexError) as exc:
            raise ModelError("Model returned no choices") from exc

        message = choice.message
        content = getattr(message, "content", None)
        tool_calls: list[ToolCall] = []

        for raw_call in getattr(message, "tool_calls", None) or []:
            parsed = cls._parse_tool_call(raw_call)
            if parsed is not None:
                tool_calls.append(parsed)

        legacy_call = getattr(message, "function_call", None)
        if not tool_calls and legacy_call is not None:
            parsed = cls._parse_tool_call(SimpleNamespace(id=None, function=legacy_call))
            if parsed is not None:
                tool_calls.append(parsed)

        usage: dict[str, int] = {}
        usage_obj = getattr(response, "usage", None)
        if usage_obj is not None:
            usage = {
                key: int(getattr(usage_obj, key, 0) or 0)
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            }

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=usage,
            raw=response,
        )
