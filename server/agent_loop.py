import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from agent_events import AgentCompleted, AgentDelta, AgentEvent, AgentLoopError
from agent_runtime import is_locally_configured, stream_local_agent

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


async def _mock_events() -> AsyncIterator[AgentEvent]:
    for index in range(0, len(MOCK_RESPONSE), 3):
        await asyncio.sleep(0.03)
        yield AgentDelta(MOCK_RESPONSE[index : index + 3])
    yield AgentCompleted(metadata={"finish_reason": "stop", "mock": True})


def _handle_upstream_event(event_name: str, data: str) -> AgentEvent | None:
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
    yield AgentCompleted(metadata={"finish_reason": payload.get("finish_reason", "stop")})


async def stream_agent(payload: dict[str, Any], request_id: str) -> AsyncIterator[AgentEvent]:
    """Yield deltas from the configured agent executor.

    Raises AgentLoopError for configuration, transport, and protocol failures.
    Callers are responsible for persisting partial content when this raises
    after some deltas were yielded."""
    config = load_config()

    if config.mock:
        async for event in _mock_events():
            yield event
        return

    if not config.url:
        async for event in stream_local_agent(payload):
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

    try:
        async with asyncio.timeout(config.max_seconds):
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
                            yield event
                    else:
                        async for event in _parse_json_fallback(response):
                            yield event
    except AgentLoopError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise AgentLoopError("agent_unreachable", f"Could not reach the agent executor: {exc}") from exc
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, TimeoutError) as exc:
        raise AgentLoopError("agent_timeout", "Agent executor timed out.") from exc
    except httpx.HTTPError as exc:
        raise AgentLoopError("agent_unreachable", f"Agent executor request failed: {exc}") from exc
