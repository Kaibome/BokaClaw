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

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def _usage_field(usage: Any, key: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _usage_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mentions_stream_options(exc: Exception) -> bool:
    message = str(exc).lower()
    return "stream_options" in message or "include_usage" in message


class OpenAICompatibleProvider:
    """Call any OpenAI-compatible ``/chat/completions`` endpoint.

    Works with OpenAI, DeepSeek, Qwen/DashScope compatible mode, vLLM, Ollama's
    OpenAI endpoint, and similar services. The key behaviour for ReAct is that
    ``tool_call.id`` is preserved and ``function.arguments`` is parsed without
    discarding the raw string.
    """

    def __init__(self, config: ModelConfig, client: OpenAI | None = None) -> None:
        self.config = config
        # Some OpenAI-compatible endpoints reject ``stream_options``; disable it
        # for this provider instance after the first rejection.
        self._stream_usage_supported = True
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
        stream_options_added = False
        extra_body = self.config.extra_body or {}
        if (
            self._stream_usage_supported
            and "stream_options" not in params
            and "stream_options" not in extra_body
        ):
            params["stream_options"] = {"include_usage": True}
            stream_options_added = True

        try:
            stream = self._create_stream(params)
        except Exception as exc:  # noqa: BLE001 - provider errors are normalized
            # Some OpenAI-compatible servers reject the parameter outright. Retry
            # once without it, but only before any chunk has been consumed.
            if not stream_options_added:
                raise ModelError(f"Model request failed: {exc}") from exc
            if _mentions_stream_options(exc):
                self._stream_usage_supported = False
            params.pop("stream_options", None)
            try:
                stream = self._create_stream(params)
            except Exception as retry_exc:  # noqa: BLE001
                raise ModelError(f"Model request failed: {retry_exc}") from retry_exc

        try:
            for chunk in stream:
                parsed = self._parse_stream_chunk(chunk)
                if parsed is not None:
                    yield parsed
        except ModelError:
            raise
        except Exception as exc:  # noqa: BLE001 - provider errors are normalized
            raise ModelError(f"Model request failed: {exc}") from exc

    def _create_stream(self, params: dict[str, Any]) -> Any:
        return self._client.chat.completions.create(**params)

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
        """Flatten provider usage into scalar counts the runtime can sum.

        Cache and reasoning counters arrive in provider-specific shapes:
        DeepSeek returns ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``,
        OpenAI nests ``cached_tokens`` under ``prompt_tokens_details``, and
        reasoning models nest ``reasoning_tokens`` under
        ``completion_tokens_details``. The runtime merges usage dicts by adding
        integers, so nested objects must be flattened here.
        """

        if usage_obj is None:
            return {}

        usage = {key: _usage_int(_usage_field(usage_obj, key)) or 0 for key in _USAGE_KEYS}

        cached = _usage_int(_usage_field(usage_obj, "prompt_cache_hit_tokens"))
        if cached is None:
            details = _usage_field(usage_obj, "prompt_tokens_details")
            cached = _usage_int(_usage_field(details, "cached_tokens"))
        if cached is not None:
            usage["cached_tokens"] = cached

        missed = _usage_int(_usage_field(usage_obj, "prompt_cache_miss_tokens"))
        if missed is None and cached is not None:
            missed = max(usage["prompt_tokens"] - cached, 0)
        if missed is not None:
            usage["cache_miss_tokens"] = missed

        reasoning_details = _usage_field(usage_obj, "completion_tokens_details")
        reasoning = _usage_int(_usage_field(reasoning_details, "reasoning_tokens"))
        if reasoning is not None:
            usage["reasoning_tokens"] = reasoning
        return usage

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
