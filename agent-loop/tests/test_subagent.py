from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest
from conftest import FakeProvider

from bokaclaw_agent.agent.runtime import AgentRuntime
from bokaclaw_agent.agent.subagent import (
    SubagentOptions,
    SubagentSpec,
    SubagentTask,
    SubagentTool,
    render_subagent_prompt,
)
from bokaclaw_agent.core.config import AgentConfig, ModelConfig
from bokaclaw_agent.core.exceptions import RunCancelled
from bokaclaw_agent.core.types import EventType, ModelResponse, ToolCall
from bokaclaw_agent.tools import ToolRegistry, tool


@tool(name="Read")
def read_file(path: str) -> str:
    """Read a workspace file.

    Args:
        path: File path to read.
    """
    return f"contents of {path}"


@tool(name="Write")
def write_file(path: str, content: str) -> str:
    """Write a workspace file.

    Args:
        path: File path to write.
        content: New contents.
    """
    return "written"


@tool(name="Read")
def slow_read(path: str) -> str:
    """Read a workspace file slowly.

    Args:
        path: File path to read.
    """
    time.sleep(0.2)
    return f"slow contents of {path}"


class CallableProvider(FakeProvider):
    """FakeProvider whose scripted item may be a callable of the messages."""

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
            raise AssertionError("CallableProvider has no scripted responses left")
        response = self.responses.pop(0)
        if callable(response):
            response = response(messages)
        if isinstance(response, Exception):
            raise response
        return response


def _spec(
    *,
    name: str = "explore",
    policy: str = "read_only",
    schema: dict[str, Any] | None = None,
    timeout: float = 5.0,
    resumable: bool = True,
) -> SubagentSpec:
    return SubagentSpec(
        name=name,
        description="Read-only exploration for tasks with noisy intermediate output.",
        system_prompt="You are an explore subagent. You cannot see the parent conversation.",
        tool_policy=policy,  # type: ignore[arg-type]
        model=ModelConfig(model="child-model"),
        default_max_turns=4,
        default_timeout_seconds=timeout,
        output_schema=schema,
        resumable=resumable,
    )


def _task_arguments(**overrides: Any) -> dict[str, Any]:
    arguments = {
        "label": "read a.py",
        "goal": "read a.py and summarize it",
        "scope": ["/ws/a.py"],
        "background": "The workspace root is /ws. Only a.py matters.",
        "questions": ["what is in a.py", "which symbols does it define"],
        "output_format": "JSON object",
    }
    arguments.update(overrides)
    return arguments


def _explore_tool_call(arguments: dict[str, Any], call_id: str = "p1") -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name="explore", arguments=arguments)],
        finish_reason="tool_calls",
    )


def _json_response(payload: dict[str, Any], **usage: int) -> ModelResponse:
    return ModelResponse(content=json.dumps(payload), finish_reason="stop", usage=usage)


def test_subagent_runs_in_isolated_context_and_returns_structured_result():
    parent_provider = FakeProvider(
        [
            _explore_tool_call(_task_arguments()),
            ModelResponse(
                content="done",
                finish_reason="stop",
                usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            ),
        ]
    )
    child_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="Read", arguments={"path": "/ws/a.py"})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            ),
            _json_response(
                {
                    "report": "a.py defines one helper.",
                    "facts": [
                        {
                            "claim": "a.py defines helper()",
                            "evidence": "/ws/a.py:1",
                            "confidence": "high",
                        }
                    ],
                    "inferences": ["helper is used elsewhere"],
                    "unknowns": ["call sites were not inspected"],
                    "sources": ["/ws/a.py", "/ws/a.py"],
                },
                prompt_tokens=20,
                completion_tokens=10,
                total_tokens=30,
            ),
        ]
    )
    events = []
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file, write_file],
        provider=parent_provider,
        on_event=events.append,
        subagents=[_spec(schema={"type": "object"})],
        subagent_options=SubagentOptions(max_calls_per_run=3),
        subagent_provider_factory=lambda _model: child_provider,
    )

    result = runtime.run("hi", session_id="s1")

    assert result.content == "done"
    assert result.usage["total_tokens"] == 48
    assert len(result.tool_results) == 1

    payload = json.loads(result.tool_results[0].content)
    assert payload["status"] == "completed"
    assert payload["report"] == "a.py defines one helper."
    assert payload["facts"] == [{"claim": "a.py defines helper()", "evidence": "/ws/a.py:1", "confidence": "high"}]
    assert payload["inferences"] == ["helper is used elsewhere"]
    assert payload["unknowns"] == ["call sites were not inspected"]
    assert payload["sources"] == ["/ws/a.py"]
    assert payload["turns"] == 2
    assert payload["isolated_chars"] == len("contents of /ws/a.py")
    assert payload["resumable"] is True
    assert payload["subagent_id"].startswith("sub_")
    assert result.tool_results[0].usage["total_tokens"] == 42
    assert result.tool_results[0].metadata["subagent"]["status"] == "completed"

    # The child gets its own system prompt and the rendered task packet; it
    # never sees the parent's "hi".
    child_first = child_provider.calls[0]["messages"]
    assert child_first[0] == {
        "role": "system",
        "content": "You are an explore subagent. You cannot see the parent conversation.",
    }
    assert "你看不到任何对话历史" in child_first[1]["content"]
    assert "read a.py and summarize it" in child_first[1]["content"]
    assert "what is in a.py" in child_first[1]["content"]
    assert "/ws/a.py" in child_first[1]["content"]
    assert {"role": "user", "content": "hi"} not in child_first
    assert [spec["function"]["name"] for spec in child_provider.calls[0]["tools"]] == ["Read"]

    # The child's intermediate tool output stays in the child context. The
    # parent only receives the structured conclusion.
    parent_final = parent_provider.calls[1]["messages"]
    assert "contents of /ws/a.py" not in json.dumps(parent_final, ensure_ascii=False)
    assert "a.py defines helper()" in json.dumps(parent_final, ensure_ascii=False)
    assert "Subagent delegations" in json.dumps(parent_final, ensure_ascii=False)
    assert "spot-check at least one fact" in json.dumps(parent_final, ensure_ascii=False)

    types = [event.type for event in events]
    assert EventType.SUBAGENT_STARTED in types
    assert EventType.SUBAGENT_EVENT in types
    assert EventType.SUBAGENT_FINISHED in types
    started = next(event for event in events if event.type == EventType.SUBAGENT_STARTED)
    finished = next(event for event in events if event.type == EventType.SUBAGENT_FINISHED)
    assert started.data["subagent_type"] == "explore"
    assert started.data["label"] == "read a.py"
    assert finished.data["status"] == "completed"
    assert finished.data["usage"]["total_tokens"] == 42
    subagent_event = next(event for event in events if event.type == EventType.SUBAGENT_EVENT)
    assert subagent_event.data["subagent_id"] == started.data["subagent_id"]


def test_resume_keeps_the_same_subagent_history():
    def resume_call(messages: list[dict[str, Any]]) -> ModelResponse:
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        first_result = json.loads(tool_messages[-1]["content"])
        return ModelResponse(
            tool_calls=[
                ToolCall(
                    id="p2",
                    name="explore",
                    arguments=_task_arguments(
                        goal="continue and name the helper",
                        questions=["what is the helper called"],
                        resume=first_result["subagent_id"],
                    ),
                )
            ],
            finish_reason="tool_calls",
        )

    parent_provider = CallableProvider(
        [
            _explore_tool_call(_task_arguments()),
            resume_call,
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    child_provider = FakeProvider(
        [
            _json_response({"report": "first done"}),
            _json_response({"report": "second done"}),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        subagents=[_spec()],
        subagent_options=SubagentOptions(max_calls_per_run=3),
        subagent_provider_factory=lambda _model: child_provider,
    )

    result = runtime.run("hi", session_id="s1")

    payloads = [json.loads(item.content) for item in result.tool_results]
    assert len(payloads) == 2
    assert payloads[0]["report"] == "first done"
    assert payloads[1]["report"] == "second done"
    assert payloads[0]["subagent_id"] == payloads[1]["subagent_id"]
    assert payloads[1]["resumable"] is True

    # The child's provider view also carries the runtime's step-budget notice;
    # it is view-only and never persisted, so compare the persisted history.
    resumed_request = [
        message
        for message in child_provider.calls[1]["messages"]
        if not (message.get("role") == "user" and str(message.get("content", "")).startswith("[Runtime context"))
    ]
    assert len(resumed_request) == 4
    assert resumed_request[0]["role"] == "system"
    assert "read a.py and summarize it" in resumed_request[1]["content"]
    assert resumed_request[2]["content"] == '{"report": "first done"}'
    assert "这是同一个子任务的续接" in resumed_request[3]["content"]
    assert "continue and name the helper" in resumed_request[3]["content"]


def test_schema_failure_returns_partial_with_raw_report():
    parent_provider = FakeProvider(
        [_explore_tool_call(_task_arguments()), ModelResponse(content="done", finish_reason="stop")]
    )
    child_provider = FakeProvider([ModelResponse(content="not json", finish_reason="stop")])
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        subagents=[_spec(schema={"type": "object"})],
        subagent_provider_factory=lambda _model: child_provider,
    )

    result = runtime.run("hi", session_id="s1")

    payload = json.loads(result.tool_results[0].content)
    assert payload["status"] == "partial"
    assert payload["report"] == "not json"
    assert "output_schema" in payload["error"]


def test_depth_and_background_limits_return_structured_failures():
    provider = FakeProvider([])
    spec = _spec()
    tool_obj = SubagentTool(
        spec,
        parent_config=AgentConfig(),
        parent_tools=ToolRegistry([read_file]),
        depth=2,
        options=SubagentOptions(max_depth=2),
        parent_provider=provider,
    ).as_tool()

    depth_result = json.loads(tool_obj.invoke({"goal": "anything"}).content)
    assert depth_result["status"] == "failed"
    assert "depth limit" in depth_result["error"]
    assert provider.calls == []

    background_tool = SubagentTool(
        _spec(),
        parent_config=AgentConfig(),
        parent_tools=ToolRegistry([read_file]),
        depth=0,
        options=SubagentOptions(max_depth=2),
        parent_provider=provider,
    ).as_tool()
    background_result = json.loads(background_tool.invoke({"goal": "anything", "run_in_background": True}).content)
    assert background_result["status"] == "failed"
    assert "Background subagents" in background_result["error"]


def test_call_budget_is_shared_across_subagent_tools():
    parent_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p1",
                        name="first",
                        arguments={"goal": "read a.py"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p2",
                        name="second",
                        arguments={"goal": "read b.py"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    child_provider = FakeProvider([_json_response({"report": "first done"})])
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        subagents=[_spec(name="first"), _spec(name="second")],
        subagent_options=SubagentOptions(max_calls_per_run=1),
        subagent_provider_factory=lambda _model: child_provider,
    )

    result = runtime.run("hi", session_id="s1")

    first_payload = json.loads(result.tool_results[0].content)
    second_payload = json.loads(result.tool_results[1].content)
    assert first_payload["status"] == "completed"
    assert second_payload["status"] == "failed"
    assert "call budget exhausted" in second_payload["error"]
    assert len(child_provider.calls) == 1


def test_run_scoped_budget_and_ledger_reset_between_runs():
    parent_provider = FakeProvider(
        [
            _explore_tool_call(_task_arguments(goal="first task")),
            ModelResponse(content="one", finish_reason="stop"),
            _explore_tool_call(_task_arguments(goal="second task"), call_id="p2"),
            ModelResponse(content="two", finish_reason="stop"),
        ]
    )
    child_provider = FakeProvider(
        [
            _json_response({"report": "first done"}),
            _json_response({"report": "second done"}),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        subagents=[_spec()],
        subagent_options=SubagentOptions(max_calls_per_run=1),
        subagent_provider_factory=lambda _model: child_provider,
    )

    first = runtime.run("hi", session_id="s1")
    second = runtime.run("again", session_id="s1")

    assert json.loads(first.tool_results[0].content)["status"] == "completed"
    assert json.loads(second.tool_results[0].content)["status"] == "completed"
    assert len(child_provider.calls) == 2
    second_parent_view = json.dumps(parent_provider.calls[3]["messages"], ensure_ascii=False)
    assert "second task" in second_parent_view
    assert "first task" not in second_parent_view


def test_token_budget_counts_non_resumable_children():
    parent_provider = FakeProvider(
        [
            _explore_tool_call(_task_arguments()),
            _explore_tool_call(_task_arguments(), call_id="p2"),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    child_provider = FakeProvider([_json_response({"report": "done"}, total_tokens=6)])
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        subagents=[_spec(resumable=False)],
        subagent_options=SubagentOptions(max_calls_per_run=3, token_budget=1),
        subagent_provider_factory=lambda _model: child_provider,
    )

    result = runtime.run("hi", session_id="s1")

    first_payload = json.loads(result.tool_results[0].content)
    second_payload = json.loads(result.tool_results[1].content)
    assert first_payload["status"] == "completed"
    assert second_payload["status"] == "failed"
    assert "token budget exhausted" in second_payload["error"]
    assert len(child_provider.calls) == 1


def test_custom_policy_and_nested_subagents_are_not_exposed():
    spec = SubagentSpec(
        name="writer",
        description="Write one file.",
        system_prompt="You are a writer.",
        tool_policy="custom",
        tool_names=("Write",),
        model=ModelConfig(model="child-model"),
    )
    parent_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="p1",
                        name="writer",
                        arguments={"goal": "write a file", "scope": ["/ws/a.py"]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    child_provider = FakeProvider([_json_response({"report": "written"})])
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file, write_file],
        provider=parent_provider,
        subagents=[spec],
        subagent_provider_factory=lambda _model: child_provider,
    )

    runtime.run("hi", session_id="s1")

    assert [item["function"]["name"] for item in child_provider.calls[0]["tools"]] == ["Write"]


def test_timeout_returns_partial_context_and_does_not_fail_parent():
    parent_provider = FakeProvider(
        [_explore_tool_call(_task_arguments()), ModelResponse(content="done", finish_reason="stop")]
    )
    child_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="Read", arguments={"path": "/ws/a.py"})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
            ),
            ModelResponse(content="too late", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[slow_read, write_file],
        provider=parent_provider,
        subagents=[_spec(timeout=0.05)],
        subagent_provider_factory=lambda _model: child_provider,
    )

    result = runtime.run("hi", session_id="s1")

    payload = json.loads(result.tool_results[0].content)
    assert payload["status"] == "timeout"
    assert payload["turns"] == 1
    assert payload["resumable"] is False
    assert payload["isolated_chars"] == len("slow contents of /ws/a.py")
    assert result.content == "done"
    assert result.usage["total_tokens"] == 6


def test_parent_cancellation_stops_the_child():
    parent_provider = FakeProvider([_explore_tool_call(_task_arguments())])
    child_provider = FakeProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(id="c1", name="Read", arguments={"path": "/ws/a.py"})],
                finish_reason="tool_calls",
            )
        ]
    )
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[slow_read],
        provider=parent_provider,
        subagents=[_spec(timeout=5.0)],
        subagent_provider_factory=lambda _model: child_provider,
    )
    cancel = threading.Event()
    timer = threading.Timer(0.05, cancel.set)
    timer.start()

    with pytest.raises(RunCancelled) as caught:
        runtime.run("hi", session_id="s1", cancel_event=cancel)

    timer.cancel()
    payload = json.loads(caught.value.result.tool_results[0].content)
    assert payload["status"] == "cancelled"
    assert payload["resumable"] is False


def test_prompt_has_the_five_part_shape():
    prompt = render_subagent_prompt(
        _spec(),
        SubagentTask(**_task_arguments()),
    )

    assert "目标：read a.py and summarize it" in prompt
    assert "范围（绝对路径 / URL / 数据源）：" in prompt
    assert "你看不到任何对话历史" in prompt
    assert "请回答以下问题：" in prompt
    assert "返回格式：" in prompt
    assert "只读：不要修改、创建或删除任何文件。" in prompt


def test_started_event_records_the_parent_tool_call_id():
    parent_provider = FakeProvider(
        [_explore_tool_call(_task_arguments(), call_id="call_parent"), ModelResponse(content="done")]
    )
    child_provider = FakeProvider([_json_response({"report": "done"})])
    events = []
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        on_event=events.append,
        subagents=[_spec()],
        subagent_provider_factory=lambda _model: child_provider,
    )

    done = runtime.run("hi", session_id="s1")

    started = next(event for event in events if event.type == EventType.SUBAGENT_STARTED)
    assert started.data["parent_tool_call_id"] == "call_parent"
    assert done.tool_results[0].tool_call_id == "call_parent"


def test_task_validator_rejects_before_budget_and_started_event():
    seen: list[SubagentTask] = []

    def validate(task: SubagentTask) -> str | None:
        seen.append(task)
        if any(scope.startswith("/etc") for scope in task.scope):
            return "scope escapes the workspace"
        return None

    parent_provider = FakeProvider(
        [
            _explore_tool_call(_task_arguments(scope=["/etc/passwd"]), call_id="p1"),
            _explore_tool_call(_task_arguments(scope=["/ws/a.py"]), call_id="p2"),
            ModelResponse(content="done", finish_reason="stop"),
        ]
    )
    child_provider = FakeProvider([_json_response({"report": "done"})])
    events = []
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        on_event=events.append,
        subagents=[_spec()],
        subagent_options=SubagentOptions(max_calls_per_run=1),
        subagent_provider_factory=lambda _model: child_provider,
        subagent_task_validator=validate,
    )

    result = runtime.run("hi", session_id="s1")

    rejected = json.loads(result.tool_results[0].content)
    accepted = json.loads(result.tool_results[1].content)
    assert rejected["status"] == "failed"
    assert rejected["error"] == "scope escapes the workspace"
    # Rejection did not consume the single allowed call.
    assert accepted["status"] == "completed"
    assert len(seen) == 2
    started = [event for event in events if event.type == EventType.SUBAGENT_STARTED]
    assert len(started) == 1


def test_task_validator_exception_becomes_a_structured_failure():
    def validate(_task: SubagentTask) -> str | None:
        raise RuntimeError("validator exploded")

    parent_provider = FakeProvider([_explore_tool_call(_task_arguments()), ModelResponse(content="done")])
    runtime = AgentRuntime(
        AgentConfig(),
        tools=[read_file],
        provider=parent_provider,
        subagents=[_spec()],
        subagent_provider_factory=lambda _model: FakeProvider([_json_response({"report": "done"})]),
        subagent_task_validator=validate,
    )

    result = runtime.run("hi", session_id="s1")

    payload = json.loads(result.tool_results[0].content)
    assert payload["status"] == "failed"
    assert "validator exploded" in payload["error"]
    assert result.content == "done"
