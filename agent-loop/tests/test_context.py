from __future__ import annotations

import pytest

from bokaclaw_agent.agent.context import ContextManager, assistant_message, normalize_input, tool_message
from bokaclaw_agent.core.types import ModelResponse, ToolCall, ToolResult


def test_normalize_input_string():
    assert normalize_input("hello") == [{"role": "user", "content": "hello"}]


def test_normalize_input_rejects_invalid_items():
    with pytest.raises(ValueError):
        normalize_input([{"content": "missing role"}])


def test_system_prompt_is_prepended_and_not_stored():
    context = ContextManager(system_prompt="Be concise.")
    context.load_history([{"role": "user", "content": "old"}])
    context.append({"role": "user", "content": "new"})

    assert context.build() == [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "old"},
        {"role": "user", "content": "new"},
    ]
    assert all(message["role"] != "system" for message in context.messages)


def test_truncation_prefers_user_turn_and_preserves_system():
    context = ContextManager(system_prompt="sys", max_history_messages=3)
    context.load_history(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
            {"role": "user", "content": "u3"},
        ]
    )

    built = context.build()
    assert built[0] == {"role": "system", "content": "sys"}
    # The newest 3 messages already start at a user turn, so all three are kept.
    assert built[1:] == [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
    ]


def test_truncation_never_drops_current_run_messages():
    context = ContextManager(system_prompt="sys", max_history_messages=1)
    context.load_history(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
    )
    context.append({"role": "user", "content": "new"})
    context.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        }
    )
    context.append({"role": "tool", "tool_call_id": "c1", "content": "ok"})

    built = context.build()
    assert built[0] == {"role": "system", "content": "sys"}
    assert [message["role"] for message in built[1:]] == ["user", "user", "assistant", "tool"]
    assert built[1]["content"] == "u2"  # history truncated to the newest message
    assert built[2]["content"] == "new"  # current run is always complete
    assert built[3]["tool_calls"][0]["id"] == "c1"
    assert built[4]["tool_call_id"] == "c1"


def test_truncation_never_orphans_tool_messages():
    context = ContextManager(max_history_messages=3)
    context.load_history(
        [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "3"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "u2"},
        ]
    )

    built = context.build()
    assert built == [{"role": "user", "content": "u2"}]


def test_assistant_message_preserves_tool_call_ids_and_raw_arguments():
    response = ModelResponse(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_1",
                name="add",
                arguments={"a": 1, "b": 2},
                raw_arguments='{"a": 1, "b": 2}',
            )
        ],
    )

    message = assistant_message(response)
    assert message["role"] == "assistant"
    assert message["content"] is None
    assert message["tool_calls"][0]["id"] == "call_1"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"a": 1, "b": 2}'


def test_tool_message_omits_name_for_wire_compatibility():
    message = tool_message(ToolResult(tool_call_id="call_1", name="add", content="3"))
    assert message == {"role": "tool", "tool_call_id": "call_1", "content": "3"}


def test_tool_result_cap_only_changes_the_model_view():
    context = ContextManager(tool_result_max_chars=300)
    context.load_history(
        [
            {"role": "user", "content": "u1"},
            {"role": "tool", "tool_call_id": "c1", "content": "h" * 500},
            {"role": "assistant", "content": "a" * 500},
        ]
    )
    context.append({"role": "tool", "tool_call_id": "c2", "content": "t" * 500})

    built = context.build()

    assert len(built[1]["content"]) == 300
    assert built[1]["content"].startswith("h")
    assert built[2]["content"] == "a" * 500
    assert len(built[3]["content"]) == 300
    # Stored messages stay complete for persistence and the next run.
    assert context.messages[1]["content"] == "h" * 500
    assert context.messages[3]["content"] == "t" * 500


def test_tool_result_cap_is_off_by_default():
    context = ContextManager()
    context.append({"role": "tool", "tool_call_id": "c1", "content": "t" * 500})

    assert context.build()[0]["content"] == "t" * 500
