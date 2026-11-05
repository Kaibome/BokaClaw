import asyncio
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from app.agent.events import (
    AgentCompleted,
    AgentDelta,
    AgentEvent,
    AgentLoopError,
    AgentToolCall,
    AgentToolResult,
)
from app.agent.runtime import is_locally_configured, stream_local_agent
from app.agent.usage import RunUsageMeter

MOCK_RESPONSE = (
    "你好！我是 BokaClaw Agent 的联调假回复。\n\n"
    "当前 AGENT_LOOP_MOCK=true，说明前端到服务端的 SSE 链路已经打通。"
    "关闭 AGENT_LOOP_MOCK 后，这里会显示 bokaclaw-agent 的实际执行结果。"
)


@dataclass
class AgentLoopConfig:
    url: str | None
    api_key: str | None
    mock: bool
    connect_timeout: float
    read_timeout: float
    max_seconds: float
    history_messages: int


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def load_config() -> AgentLoopConfig:
    url = (os.environ.get("AGENT_LOOP_URL") or "").strip().rstrip("/") or None
    mock = os.environ.get("AGENT_LOOP_MOCK", "").lower() in {"1", "true", "yes", "on"}
    return AgentLoopConfig(
        url=url,
        api_key=(os.environ.get("AGENT_LOOP_API_KEY") or "").strip() or None,
        mock=mock,
        connect_timeout=_env_float("AGENT_LOOP_CONNECT_TIMEOUT_SECONDS", 5.0),
        read_timeout=_env_float("AGENT_LOOP_READ_TIMEOUT_SECONDS", 120.0),
        max_seconds=_env_float("AGENT_LOOP_MAX_SECONDS", 300.0),
        history_messages=_env_int("AGENT_LOOP_HISTORY_MESSAGES", 40),
    )


def is_configured() -> bool:
    config = load_config()
    return config.mock or bool(config.url) or is_locally_configured()


def sse_event(event: str, data: dict[str, Any], event_id: str | None = None) -> str:
    """Encode one SSE frame. Data is JSON; the final blank line terminates it."""
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    for data_line in json.dumps(data, ensure_ascii=False, default=str).splitlines() or [""]:
        lines.append(f"data: {data_line}")
    return "\n".join(lines) + "\n\n"


def sse_comment(text: str = "ping") -> str:
    return f": {text}\n\n"


async def _mock_events(cancel_event: threading.Event | None = None) -> AsyncIterator[AgentEvent]:
    for index in range(0, len(MOCK_RESPONSE), 3):
        if cancel_event is not None and cancel_event.is_set():
            raise AgentLoopError("agent_cancelled", "Agent run was cancelled.")
        await asyncio.sleep(0.03)
        yield AgentDelta(MOCK_RESPONSE[index : index + 3])
    yield AgentCompleted(metadata={"finish_reason": "stop", "mock": True})


def _parse_json_object(event_name: str, data: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AgentLoopError(
            "agent_protocol_error",
            f"Agent executor sent invalid JSON in {event_name}.",
        ) from exc
    if not isinstance(payload, dict):
        raise AgentLoopError(
            "agent_protocol_error",
            f"Agent executor sent a non-object payload in {event_name}.",
        )
    return payload


def _as_int(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tool_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _handle_tool_event(event_name: str, payload: dict[str, Any]) -> AgentEvent | None:
    tool_call_id = payload.get("tool_call_id")
    name = payload.get("name")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    if not isinstance(name, str) or not name:
        return None

    iteration = _as_int(payload.get("iteration"), 0) or 0
    if event_name in {"tool.call", "tool_call"}:
        raw_arguments = payload.get("raw_arguments")
        arguments_error = payload.get("arguments_error")
        return AgentToolCall(
            tool_call_id=tool_call_id,
            name=name,
            iteration=iteration,
            arguments=_tool_arguments(payload.get("arguments")),
            raw_arguments=raw_arguments if isinstance(raw_arguments, str) else None,
            arguments_error=arguments_error if isinstance(arguments_error, str) else None,
        )

    is_error = bool(payload.get("is_error"))
    status = payload.get("status")
    if status not in {"completed", "failed", "aborted"}:
        status = "failed" if is_error else "completed"
    return AgentToolResult(
        tool_call_id=tool_call_id,
        name=name,
        content=_tool_text(payload.get("content")),
        is_error=is_error,
        iteration=iteration,
        status=status,
        duration_ms=_as_int(payload.get("duration_ms")),
    )


def _handle_upstream_event(event_name: str, data: str) -> AgentEvent | None:
    if event_name in {"tool.call", "tool_call", "tool.result", "tool_result"}:
        return _handle_tool_event(event_name, _parse_json_object(event_name, data))

    if event_name == "message.delta":
        try:
            payload = json.loads(data)
        except json.JSONDecodeError as exc:
            raise AgentLoopError("agent_protocol_error", "Agent executor sent invalid JSON in message.delta.") from exc
        delta = payload.get("delta")
        if not isinstance(delta, str) or not delta:
            return None
        return AgentDelta(delta)

    if event_name == "message.completed":
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            payload = {}
        metadata = {key: payload[key] for key in ("usage", "finish_reason", "run_id") if key in payload}
        nested = payload.get("metadata")
        if isinstance(nested, dict):
            for key in ("usage", "finish_reason", "run_id"):
                if key in nested and key not in metadata:
                    metadata[key] = nested[key]
        return AgentCompleted(metadata=metadata)

    if event_name == "error":
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            payload = {"detail": data}
        detail = payload.get("detail") or payload.get("message") or "Agent executor returned an error."
        raise AgentLoopError("agent_upstream_error", str(detail))

    return None


async def _parse_upstream_sse(response: httpx.Response) -> AsyncIterator[AgentEvent]:
    event_name = "message"
    data_lines: list[str] = []

    async for raw_line in response.aiter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                parsed = _handle_upstream_event(event_name, "\n".join(data_lines))
                if parsed is not None:
                    yield parsed
            event_name = "message"
            data_lines = []
            continue

        if line.startswith(":"):
            continue

        field_name, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field_name == "event":
            event_name = value
        elif field_name == "data":
            data_lines.append(value)

    if data_lines:
        parsed = _handle_upstream_event(event_name, "\n".join(data_lines))
        if parsed is not None:
            yield parsed


async def _parse_json_fallback(response: httpx.Response) -> AsyncIterator[AgentEvent]:
    text = (await response.aread()).decode("utf-8", "replace")
    if not text.strip():
        raise AgentLoopError("agent_protocol_error", "Agent executor returned an empty response.")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentLoopError("agent_protocol_error", "Agent executor returned neither SSE nor JSON.") from exc

    content = payload.get("content") or payload.get("message")
    if not isinstance(content, str) or not content:
        raise AgentLoopError("agent_protocol_error", "Agent executor JSON response is missing content.")
    yield AgentDelta(content)
    metadata: dict[str, Any] = {"finish_reason": payload.get("finish_reason", "stop")}
    if "usage" in payload:
        metadata["usage"] = payload["usage"]
    yield AgentCompleted(metadata=metadata)


def _cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


async def stream_agent(
    payload: dict[str, Any],
    request_id: str,
    *,
    cancel_event: threading.Event | None = None,
    max_seconds: float | None = None,
    usage_meter: RunUsageMeter | None = None,
) -> AsyncIterator[AgentEvent]:
    """Yield deltas from the configured agent executor.

    Raises AgentLoopError for configuration, transport, and protocol failures.
    Callers are responsible for persisting partial content when this raises
    after some deltas were yielded. ``cancel_event`` is polled between events
    so a cancelled run stops without waiting for the next model token."""
    config = load_config()

    if usage_meter is not None:
        usage_meter.set_transport("mock" if config.mock else ("local" if not config.url else "remote"))

    if config.mock:
        async for event in _mock_events(cancel_event):
            yield event
        return

    if not config.url:
        async for event in stream_local_agent(
            payload,
            cancel_event=cancel_event,
            max_seconds=max_seconds,
            usage_meter=usage_meter,
        ):
            yield event
        return

    headers = {
        "Accept": "text/event-stream",
        "Content-Type": "application/json",
        "X-Request-ID": request_id,
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    timeout = httpx.Timeout(
        connect=config.connect_timeout,
        read=config.read_timeout,
        write=10.0,
        pool=5.0,
    )
    deadline = max_seconds if max_seconds is not None else config.max_seconds

    try:
        async with asyncio.timeout(deadline):
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    "POST",
                    f"{config.url}/invoke",
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")[:500]
                        raise AgentLoopError(
                            "agent_upstream_error",
                            f"Agent executor returned {response.status_code}: {body}",
                        )

                    content_type = response.headers.get("content-type", "")
                    if "text/event-stream" in content_type:
                        async for event in _parse_upstream_sse(response):
                            if _cancelled(cancel_event):
                                raise AgentLoopError("agent_cancelled", "Agent run was cancelled.")
                            yield event
                    else:
                        async for event in _parse_json_fallback(response):
                            if _cancelled(cancel_event):
                                raise AgentLoopError("agent_cancelled", "Agent run was cancelled.")
                            yield event
    except AgentLoopError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise AgentLoopError("agent_unreachable", f"Could not reach the agent executor: {exc}") from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, TimeoutError) as exc:
        raise AgentLoopError("agent_timeout", "Agent executor timed out.") from exc
    except httpx.HTTPError as exc:
        raise AgentLoopError("agent_unreachable", f"Agent executor request failed: {exc}") from exc
