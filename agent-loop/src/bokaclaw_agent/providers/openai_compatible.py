"""OpenAI Chat Completions provider that preserves tool-call ids."""

from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from openai import OpenAI

from ..core.config import ModelConfig
from ..core.exceptions import ConfigurationError, ModelError
from ..core.types import ModelResponse, ModelStreamChunk, ToolCall, ToolCallDelta
from ..core.utils import parse_tool_arguments


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

    def _build_params(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        tool_choice: str | dict[str, Any] | None,
        **kwargs: Any,
    ) -> dict[str, Any]:
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
        return params

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ) -> ModelResponse:
        params = self._build_params(messages, tools=tools, tool_choice=tool_choice, **kwargs)

        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:  # noqa: BLE001 - provider errors are normalized
            raise ModelError(f"Model request failed: {exc}") from exc

        return self._parse_response(response)

    def stream_complete(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = "auto",
        **kwargs: Any,
    ) -> Iterator[ModelStreamChunk]:
        """Yield the assistant turn incrementally (``stream=True``).

        Tool calls arrive as fragments split across chunks; the runtime
        reassembles them by ``index`` before executing the tool.
        """
        params = self._build_params(messages, tools=tools, tool_choice=tool_choice, **kwargs)
        params["stream"] = True

        try:
            stream = self._client.chat.completions.create(**params)
            for chunk in stream:
                parsed = self._parse_stream_chunk(chunk)
                if parsed is not None:
                    yield parsed
        except ModelError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider errors are normalized
            raise ModelError(f"Model request failed: {exc}") from exc

    @staticmethod
    def _parse_arguments(raw_arguments: Any) -> tuple[dict[str, Any], str | None, str | None]:
        return parse_tool_arguments(raw_arguments)

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
    def _parse_stream_chunk(cls, chunk: Any) -> ModelStreamChunk | None:
        usage = cls._parse_usage(getattr(chunk, "usage", None))
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return ModelStreamChunk(usage=usage) if usage else None

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None) if delta is not None else None

        tool_calls: list[ToolCallDelta] = []
        for raw_call in getattr(delta, "tool_calls", None) or []:
            function = getattr(raw_call, "function", None)
            tool_calls.append(
                ToolCallDelta(
                    index=int(getattr(raw_call, "index", 0) or 0),
                    id=getattr(raw_call, "id", None) or None,
                    name=(getattr(function, "name", None) if function is not None else None) or None,
                    arguments=(getattr(function, "arguments", None) if function is not None else None) or "",
                )
            )

        finish_reason = getattr(choice, "finish_reason", None)
        if content is None and not tool_calls and finish_reason is None and not usage:
            return None
        return ModelStreamChunk(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
        )

    @staticmethod
    def _parse_usage(usage_obj: Any) -> dict[str, int]:
        if usage_obj is None:
            return {}
        return {
            key: int(getattr(usage_obj, key, 0) or 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }

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

        usage = cls._parse_usage(getattr(response, "usage", None))

        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=getattr(choice, "finish_reason", None),
            usage=usage,
            raw=response,
        )
