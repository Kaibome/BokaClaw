"""Tests for the in-process agent runtime wrapper in `server/app/agent/runtime.py`.

The ``bokaclaw-agent`` SDK is replaced with a fake, so these tests cover the
server-side contract only: model resolution from mem0/env, payload validation,
memory tool scoping, and the mapping from SDK failures to ``AgentLoopError``.
"""

import os
import sys
import types

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.runtime as agent_runtime  # noqa: E402
from app.agent.events import (  # noqa: E402
    AgentCompleted,
    AgentDelta,
    AgentLoopError,
    AgentToolCall,
    AgentToolResult,
)


class FakeMaxIterationsExceeded(Exception):
    pass


class FakeNoProgressError(Exception):
    pass


class FakeConfigurationError(Exception):
    pass


class FakeModelConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeAgentConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeTool:
    def __init__(self, fn, name=None):
        self.fn = fn
        self.name = name or fn.__name__


class FakeRuntime:
    def __init__(self, sdk, config, tools, on_event=None):
        self.sdk = sdk
        self.config = config
        self.tools = list(tools)
        self.on_event = on_event
        self.run_calls = []
        self.stream_calls = []
        self.closed = False

    def run(self, messages, **kwargs):
        self.run_calls.append((messages, kwargs))
        if self.sdk.delay:
            import time

            time.sleep(self.sdk.delay)
        if self.sdk.error is not None:
            raise self.sdk.error
        return self.sdk.result

    def stream(self, messages, **kwargs):
        self.stream_calls.append((messages, kwargs))
        for event in self.sdk.events:
            if self.on_event is not None:
                self.on_event(event)
        if self.sdk.delay:
            import time

            time.sleep(self.sdk.delay)
        if self.sdk.error is not None:
            raise self.sdk.error
        content = self.sdk.result.content or ""
        for index in range(0, len(content), 5):
            yield content[index : index + 5]
        return self.sdk.result

    def close(self):
        self.closed = True


class FakeSDK:
    MaxIterationsExceeded = FakeMaxIterationsExceeded
    NoProgressError = FakeNoProgressError
    ConfigurationError = FakeConfigurationError
    ModelConfig = FakeModelConfig
    AgentConfig = FakeAgentConfig

    def __init__(self, result=None, error=None, delay=0.0):
        self.result = result
        self.error = error
        self.delay = delay
        self.tools = []
        self.runtimes = []
        self.events = []

    def tool(self, fn, *, name=None):
        wrapped = FakeTool(fn, name=name)
        self.tools.append(wrapped)
        return wrapped

    def AgentRuntime(self, config, *, tools=(), on_event=None):
        runtime = FakeRuntime(self, config, tools, on_event=on_event)
        self.runtimes.append(runtime)
        return runtime


def _result(content="hello", usage=None, stop_reason="final"):
    return types.SimpleNamespace(
        content=content,
        usage=usage or {},
        stop_reason=stop_reason,
    )


def _config(**overrides):
    values = {
        "model": "test-model",
        "api_key": "test-key",
        "base_url": None,
        "temperature": 0.2,
        "max_tokens": None,
        "request_timeout": 60.0,
        "max_retries": 2,
        "system_prompt": "system",
        "max_iterations": 5,
        "max_seconds": 5.0,
        # Most tests focus on streaming; research reuse has its own tests.
        "research_memory": False,
    }
    values.update(overrides)
    return agent_runtime.LocalAgentConfig(**values)


@pytest.fixture(autouse=True)
def clean_agent_env(monkeypatch):
    for name in (
        "AGENT_MODEL",
        "AGENT_API_KEY",
        "AGENT_BASE_URL",
        "AGENT_SYSTEM_PROMPT",
        "AGENT_TEMPERATURE",
        "AGENT_MAX_TOKENS",
        "AGENT_MAX_ITERATIONS",
        "AGENT_FINAL_ANSWER_ON_MAX_ITERATIONS",
        "AGENT_LOOP_MAX_SECONDS",
        "AGENT_REQUEST_TIMEOUT_SECONDS",
        "AGENT_MAX_RETRIES",
        "AGENT_CONTEXT_MAX_TOKENS",
        "AGENT_CONTEXT_KEEP_RECENT_TOKENS",
        "AGENT_CONTEXT_COMPRESSION",
        "AGENT_CONTEXT_DEBUG",
        "AGENT_TOOL_RESULT_MAX_CHARS",
        "AGENT_FACT_LEDGER",
        "AGENT_FACT_LEDGER_MAX_CHARS",
        "AGENT_STEP_BUDGET_NOTICE",
        "AGENT_PROGRESS_GUARD",
        "AGENT_PROGRESS_NUDGE_AFTER",
        "AGENT_PROGRESS_STOP_AFTER",
        "AGENT_RESEARCH_MEMORY",
        "AGENT_RESEARCH_MEMORY_TTL_DAYS",
        "AGENT_RESEARCH_MEMORY_MIN_SCORE",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_API_BASE",
    ):
        monkeypatch.delenv(name, raising=False)
    # Keep these tests focused on the memory tools; dev tools have their own suite.
    monkeypatch.setenv("AGENT_DEV_TOOLS", "false")


# ---- configuration resolution -------------------------------------------------


def test_no_model_is_unconfigured(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})

    assert agent_runtime.load_local_config() is None


def test_reuses_mem0_openai_config(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "openai", "config": {"model": "gpt-test", "api_key": "sk-mem0"}},
    )

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.model == "gpt-test"
    assert config.api_key == "sk-mem0"
    assert config.base_url is None


def test_reuses_mem0_deepseek_config_with_default_base_url(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "deepseek", "config": {"model": "deepseek-chat", "api_key": "ds-key"}},
    )

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.api_key == "ds-key"
    assert config.base_url == "https://api.deepseek.com"


def test_deepseek_without_key_or_explicit_base_url_is_unconfigured(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "deepseek", "config": {"model": "deepseek-chat"}},
    )

    assert agent_runtime.load_local_config() is None


def test_explicit_env_wins_over_mem0(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "openai", "config": {"model": "gpt-test", "api_key": "sk-mem0"}},
    )
    monkeypatch.setenv("AGENT_MODEL", "local-model")
    monkeypatch.setenv("AGENT_API_KEY", "local-key")
    monkeypatch.setenv("AGENT_BASE_URL", "http://localhost:11434/v1")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert (config.model, config.api_key, config.base_url) == (
        "local-model",
        "local-key",
        "http://localhost:11434/v1",
    )


def test_explicit_model_allows_unsupported_mem0_provider(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "anthropic", "config": {"model": "claude", "api_key": "ak"}},
    )
    monkeypatch.setenv("AGENT_MODEL", "qwen-plus")
    monkeypatch.setenv("AGENT_BASE_URL", "http://localhost:8000/v1")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.model == "qwen-plus"
    assert config.api_key == "local"
    assert config.base_url == "http://localhost:8000/v1"


def test_unsupported_mem0_provider_without_override_is_unconfigured(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "anthropic", "config": {"model": "claude", "api_key": "ak"}},
    )

    assert agent_runtime.load_local_config() is None


def test_missing_key_and_base_url_is_unconfigured(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "_current_llm_config",
        lambda: {"provider": "openai", "config": {"model": "gpt-test"}},
    )

    assert agent_runtime.load_local_config() is None


def test_env_settings_are_applied(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_MAX_ITERATIONS", "3")
    monkeypatch.setenv("AGENT_LOOP_MAX_SECONDS", "42")
    monkeypatch.setenv("AGENT_SYSTEM_PROMPT", "custom")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.max_iterations == 3
    assert config.max_seconds == 42
    assert config.system_prompt == "custom"


def test_context_budgets_default_on_for_the_server(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_max_tokens == 32_000
    assert config.context_keep_recent_tokens is None
    assert config.context_compression == "summary"
    assert config.tool_result_max_chars == 16_000
    assert config.final_answer_on_max_iterations is True
    assert config.fact_ledger is True
    assert config.fact_ledger_max_chars == 2_500
    assert config.step_budget_notice is True
    assert config.progress_guard is True
    assert config.progress_nudge_after == 3
    assert config.progress_stop_after == 5


def test_progress_guard_can_be_disabled_and_tuned(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_PROGRESS_GUARD", "false")
    monkeypatch.setenv("AGENT_PROGRESS_NUDGE_AFTER", "2")
    monkeypatch.setenv("AGENT_PROGRESS_STOP_AFTER", "4")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.progress_guard is False
    assert config.progress_nudge_after == 2
    assert config.progress_stop_after == 4


def test_progress_stop_is_clamped_to_the_nudge_threshold(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_PROGRESS_NUDGE_AFTER", "6")
    monkeypatch.setenv("AGENT_PROGRESS_STOP_AFTER", "2")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.progress_nudge_after == 6
    # A stop before the nudge would make the SDK config invalid; clamp instead.
    assert config.progress_stop_after == 6


def test_research_memory_defaults_and_can_be_tuned(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.research_memory is True
    assert config.research_memory_ttl_days == 30
    assert config.research_memory_min_score == 0.5

    monkeypatch.setenv("AGENT_RESEARCH_MEMORY", "false")
    monkeypatch.setenv("AGENT_RESEARCH_MEMORY_TTL_DAYS", "7")
    monkeypatch.setenv("AGENT_RESEARCH_MEMORY_MIN_SCORE", "0.75")

    tuned = agent_runtime.load_local_config()

    assert tuned is not None
    assert tuned.research_memory is False
    assert tuned.research_memory_ttl_days == 7
    assert tuned.research_memory_min_score == 0.75


def test_research_memory_settings_are_clamped(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_RESEARCH_MEMORY_TTL_DAYS", "0")
    monkeypatch.setenv("AGENT_RESEARCH_MEMORY_MIN_SCORE", "2")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.research_memory_ttl_days == 1
    assert config.research_memory_min_score == 1.0


def test_context_budgets_can_be_overridden(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_CONTEXT_MAX_TOKENS", "8000")
    monkeypatch.setenv("AGENT_CONTEXT_KEEP_RECENT_TOKENS", "2000")
    monkeypatch.setenv("AGENT_CONTEXT_COMPRESSION", "truncate")
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", "1000")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_max_tokens == 8000
    assert config.context_keep_recent_tokens == 2000
    assert config.context_compression == "truncate"
    assert config.tool_result_max_chars == 1000


def test_fact_ledger_and_step_notice_can_be_disabled(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_FACT_LEDGER", "false")
    monkeypatch.setenv("AGENT_FACT_LEDGER_MAX_CHARS", "100")
    monkeypatch.setenv("AGENT_STEP_BUDGET_NOTICE", "off")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.fact_ledger is False
    # The SDK requires at least 256 characters; the server clamps instead of failing the run.
    assert config.fact_ledger_max_chars == 256
    assert config.step_budget_notice is False


def test_step_limit_final_answer_can_be_disabled(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_FINAL_ANSWER_ON_MAX_ITERATIONS", "false")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.final_answer_on_max_iterations is False


@pytest.mark.parametrize("value", ["0", "off", "none", "disabled", "false"])
def test_context_budgets_can_be_disabled(monkeypatch, value):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_CONTEXT_MAX_TOKENS", value)
    monkeypatch.setenv("AGENT_TOOL_RESULT_MAX_CHARS", value)

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_max_tokens is None
    assert config.tool_result_max_chars is None


def test_keep_recent_budget_is_clamped_for_a_valid_sdk_config(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_CONTEXT_MAX_TOKENS", "1024")
    monkeypatch.setenv("AGENT_CONTEXT_KEEP_RECENT_TOKENS", "5000")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_keep_recent_tokens == 768


def test_negative_keep_recent_budget_is_clamped_to_zero(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_CONTEXT_KEEP_RECENT_TOKENS", "-5")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_keep_recent_tokens == 0


def test_unknown_compression_mode_falls_back_to_summary(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_CONTEXT_COMPRESSION", "shrink")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_compression == "summary"


def test_invalid_budget_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "m")
    monkeypatch.setenv("AGENT_API_KEY", "k")
    monkeypatch.setenv("AGENT_CONTEXT_MAX_TOKENS", "lots")

    config = agent_runtime.load_local_config()

    assert config is not None
    assert config.context_max_tokens == 32_000


# ---- payload handling ---------------------------------------------------------


def test_build_messages_filters_history_and_appends_user_turn():
    messages = agent_runtime.build_messages(
        {
            "history": [
                {"role": "user", "content": "first"},
                {"role": "tool", "content": "ignored"},
                {"role": "assistant", "content": None},
                {"role": "assistant", "content": "reply"},
                "not-a-dict",
            ],
            "message": "second",
        }
    )

    assert messages == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]


def test_build_messages_forwards_persisted_tool_history():
    messages = agent_runtime.build_messages(
        {
            "history": [
                {"role": "user", "content": "compare qwen models"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "WebFetch", "arguments": '{"url": "https://example.com"}'},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": '{"url": "https://example.com", "content": "page"}',
                },
                {"role": "assistant", "content": "answer"},
            ],
            "message": "follow up",
        }
    )

    assert messages == [
        {"role": "user", "content": "compare qwen models"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "WebFetch", "arguments": '{"url": "https://example.com"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"url": "https://example.com", "content": "page"}',
        },
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "follow up"},
    ]


def test_build_messages_drops_orphan_tool_results():
    messages = agent_runtime.build_messages(
        {
            "history": [
                {"role": "tool", "tool_call_id": "missing", "content": "stale"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
                        {"id": "bad", "type": "function", "function": {}},
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "file"},
                {"role": "tool", "tool_call_id": "bad", "content": "dropped"},
            ],
            "message": "next",
        }
    )

    assert [message["role"] for message in messages] == ["assistant", "tool", "user"]
    assert messages[0]["tool_calls"] == [
        {"id": "call_1", "type": "function", "function": {"name": "Read", "arguments": "{}"}}
    ]
    assert messages[1] == {"role": "tool", "tool_call_id": "call_1", "content": "file"}


def test_build_messages_keeps_assistant_text_without_valid_tool_calls():
    messages = agent_runtime.build_messages(
        {
            "history": [
                {"role": "assistant", "content": "partial answer", "tool_calls": [{"id": "x"}]},
            ],
            "message": "next",
        }
    )

    assert messages == [
        {"role": "assistant", "content": "partial answer"},
        {"role": "user", "content": "next"},
    ]


def test_build_messages_requires_a_message():
    with pytest.raises(AgentLoopError) as excinfo:
        agent_runtime.build_messages({"history": []})

    assert excinfo.value.code == "agent_protocol_error"


# ---- memory tools -------------------------------------------------------------


def test_memory_tools_require_a_user_id():
    sdk = FakeSDK()

    assert agent_runtime._memory_tools(sdk, None) == []
    assert agent_runtime._memory_tools(sdk, "") == []
    assert sdk.tools == []


def test_memory_tools_are_scoped_to_the_user(monkeypatch):
    calls = {}

    class FakeMemory:
        def search(self, **kwargs):
            calls["search"] = kwargs
            return {"results": [{"memory": "likes tea"}]}

        def add(self, **kwargs):
            calls["add"] = kwargs
            return {"results": []}

    fake_server_state = types.SimpleNamespace(get_memory_instance=lambda: FakeMemory())
    monkeypatch.setitem(sys.modules, "app.core.server_state", fake_server_state)

    sdk = FakeSDK()
    tools = agent_runtime._memory_tools(sdk, "user-1")

    assert [type(tool) for tool in tools] == [FakeTool, FakeTool]

    search, save = (tool.fn for tool in tools)
    search(query="tea", limit=99)
    save(content="The user likes tea.")

    assert calls["search"] == {"query": "tea", "filters": {"user_id": "user-1"}, "top_k": 20}
    assert calls["add"] == {
        "messages": [{"role": "user", "content": "The user likes tea."}],
        "user_id": "user-1",
    }


# ---- streaming ----------------------------------------------------------------


def _payload(**overrides):
    payload = {
        "conversation_id": "conv-1",
        "message": "hello",
        "history": [{"role": "assistant", "content": "hi"}],
        "user_id": "user-1",
        "run_id": "run-1",
    }
    payload.update(overrides)
    return payload


async def _collect(payload):
    return [event async for event in agent_runtime.stream_local_agent(payload)]


@pytest.mark.asyncio
async def test_stream_local_agent_emits_final_answer(monkeypatch):
    sdk = FakeSDK(result=_result("final answer", usage={"total_tokens": 7}))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    events = await _collect(_payload())

    deltas = [event.delta for event in events if isinstance(event, AgentDelta)]
    assert "".join(deltas) == "final answer"
    assert len(deltas) > 1
    assert events[-1] == AgentCompleted(
        metadata={"usage": {"total_tokens": 7}, "finish_reason": "final", "run_id": "run-1"}
    )
    assert len(sdk.runtimes) == 1
    runtime = sdk.runtimes[0]
    assert runtime.stream_calls == [
        (
            [
                {"role": "assistant", "content": "hi"},
                {"role": "user", "content": "hello"},
            ],
            {"session_id": "conv-1", "user_id": "user-1"},
        )
    ]
    assert len(runtime.tools) == 2
    assert runtime.closed is True
    assert runtime.config.kwargs["model"].kwargs["model"] == "test-model"


@pytest.mark.asyncio
async def test_stream_local_agent_forwards_tool_events(monkeypatch):
    sdk = FakeSDK(result=_result("done"))
    sdk.events = [
        types.SimpleNamespace(
            type="tool_call",
            iteration=1,
            data={
                "tool_call_id": "call_1",
                "name": "Read",
                "arguments": {"path": "README.md"},
                "raw_arguments": '{"path": "README.md"}',
                "arguments_error": None,
            },
        ),
        types.SimpleNamespace(
            type="tool_result",
            iteration=1,
            data={
                "tool_call_id": "call_1",
                "name": "Read",
                "content": "hello",
                "is_error": False,
                "status": "completed",
            },
        ),
    ]
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    events = await _collect(_payload())

    tool_events = [event for event in events if isinstance(event, (AgentToolCall, AgentToolResult))]
    assert [type(event) for event in tool_events] == [AgentToolCall, AgentToolResult]
    call, result = tool_events
    assert call.tool_call_id == "call_1"
    assert call.name == "Read"
    assert call.iteration == 1
    assert call.arguments == {"path": "README.md"}
    assert call.raw_arguments == '{"path": "README.md"}'
    assert result.tool_call_id == "call_1"
    assert result.content == "hello"
    assert result.is_error is False
    assert result.status == "completed"
    assert sdk.runtimes[0].on_event is not None


@pytest.mark.asyncio
async def test_stream_local_agent_without_user_has_no_tools(monkeypatch):
    sdk = FakeSDK(result=_result())
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    await _collect(_payload(user_id=None))

    assert sdk.runtimes[0].tools == []


@pytest.mark.asyncio
async def test_stream_local_agent_requires_configuration(monkeypatch):
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: None)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_not_configured"


@pytest.mark.asyncio
async def test_stream_local_agent_maps_timeout(monkeypatch):
    sdk = FakeSDK(result=_result(), delay=0.2)
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config(max_seconds=0.01))
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_timeout"
    assert sdk.runtimes[0].closed is True


@pytest.mark.asyncio
async def test_stream_local_agent_maps_max_iterations(monkeypatch):
    sdk = FakeSDK(error=FakeMaxIterationsExceeded("too many"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_upstream_error"


@pytest.mark.asyncio
async def test_stream_local_agent_maps_no_progress(monkeypatch):
    sdk = FakeSDK(error=FakeNoProgressError("stalled"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_no_progress"


@pytest.mark.asyncio
async def test_stream_local_agent_injects_prior_research(monkeypatch):
    sdk = FakeSDK(result=_result("answer"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config(research_memory=True))
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)
    calls = {}

    def fake_block(user_id, question, **kwargs):
        calls["args"] = (user_id, question, kwargs)
        return "## Reusable research\nA prior conclusion."

    monkeypatch.setattr(agent_runtime, "prior_research_block", fake_block)

    await _collect(_payload(message="a similar question"))

    prompt = sdk.runtimes[0].config.kwargs["system_prompt"]
    assert prompt.startswith("system")
    assert "## Reusable research" in prompt
    assert calls["args"] == (
        "user-1",
        "a similar question",
        {"ttl_days": 30, "min_score": 0.5},
    )


@pytest.mark.asyncio
async def test_stream_local_agent_skips_prior_research_without_a_user(monkeypatch):
    sdk = FakeSDK(result=_result("answer"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config(research_memory=True))
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("no user should not query research memory")

    monkeypatch.setattr(agent_runtime, "prior_research_block", unexpected)

    await _collect(_payload(user_id=None))

    assert sdk.runtimes[0].config.kwargs["system_prompt"] == "system"


@pytest.mark.asyncio
async def test_stream_local_agent_survives_a_research_lookup_failure(monkeypatch):
    sdk = FakeSDK(result=_result("answer"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config(research_memory=True))
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    def broken(*_args, **_kwargs):
        raise RuntimeError("memory offline")

    monkeypatch.setattr(agent_runtime, "prior_research_block", broken)

    events = await _collect(_payload())

    assert isinstance(events[-1], AgentCompleted)
    assert all(isinstance(event, AgentDelta) for event in events[:-1])
    assert sdk.runtimes[0].config.kwargs["system_prompt"] == "system"


@pytest.mark.asyncio
async def test_stream_local_agent_maps_configuration_error(monkeypatch):
    sdk = FakeSDK(error=FakeConfigurationError("bad key"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_not_configured"
    assert excinfo.value.detail == "bad key"


@pytest.mark.asyncio
async def test_stream_local_agent_maps_unexpected_errors(monkeypatch):
    sdk = FakeSDK(error=RuntimeError("boom"))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_upstream_error"


@pytest.mark.asyncio
async def test_stream_local_agent_rejects_empty_content(monkeypatch):
    sdk = FakeSDK(result=_result("   "))
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)

    with pytest.raises(AgentLoopError) as excinfo:
        await _collect(_payload())

    assert excinfo.value.code == "agent_protocol_error"


def test_missing_sdk_is_reported_as_not_configured(monkeypatch):
    def _raise():
        raise AgentLoopError("agent_not_configured", "not installed")

    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", _raise)

    assert agent_runtime.is_locally_configured() is False


def test_build_runtime_registers_dev_tools_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DEV_TOOLS", "true")
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path))
    sdk = FakeSDK()

    agent_runtime.build_runtime(sdk, _config(), {"user_id": None, "message": "hi"})

    names = {tool.name for tool in sdk.tools}
    assert {"Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "WebFetch", "WebSearch"} <= names
    assert "Bash" not in names
    assert "search_memories" not in names


def test_build_runtime_forwards_context_settings():
    sdk = FakeSDK()
    config = _config(
        context_max_tokens=8000,
        context_keep_recent_tokens=2000,
        context_compression="truncate",
        tool_result_max_chars=1000,
        final_answer_on_max_iterations=False,
        fact_ledger=False,
        fact_ledger_max_chars=1200,
        step_budget_notice=False,
        progress_guard=False,
        progress_nudge_after=2,
        progress_stop_after=4,
    )

    agent_runtime.build_runtime(sdk, config, {"user_id": None, "message": "hi"})

    kwargs = sdk.runtimes[0].config.kwargs
    assert kwargs["final_answer_on_max_iterations"] is False
    assert kwargs["context_max_tokens"] == 8000
    assert kwargs["context_keep_recent_tokens"] == 2000
    assert kwargs["context_compression"] == "truncate"
    assert kwargs["tool_result_max_chars"] == 1000
    assert kwargs["fact_ledger"] is False
    assert kwargs["fact_ledger_max_chars"] == 1200
    assert kwargs["step_budget_notice"] is False
    assert kwargs["progress_guard"] is False
    assert kwargs["progress_nudge_after"] == 2
    assert kwargs["progress_stop_after"] == 4


def test_context_debug_is_on_by_default(monkeypatch, caplog):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    config = agent_runtime.load_local_config()

    with caplog.at_level("INFO", logger="app.agent.context"):
        agent_runtime.build_runtime(FakeSDK(), config, {"user_id": None, "message": "hi"})

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "agent context config:" in messages[0]


def test_context_debug_can_be_disabled(monkeypatch, caplog):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_CONTEXT_DEBUG", "0")
    config = agent_runtime.load_local_config()

    with caplog.at_level("INFO", logger="app.agent.context"):
        agent_runtime.build_runtime(FakeSDK(), config, {"user_id": None, "message": "hi"})

    assert caplog.records == []


def test_context_debug_logs_the_resolved_budget(monkeypatch, caplog):
    monkeypatch.setattr(agent_runtime, "_current_llm_config", lambda: {})
    monkeypatch.setenv("AGENT_MODEL", "test-model")
    monkeypatch.setenv("AGENT_API_KEY", "test-key")
    monkeypatch.setenv("AGENT_CONTEXT_DEBUG", "1")
    config = agent_runtime.load_local_config()

    with caplog.at_level("INFO", logger="app.agent.context"):
        agent_runtime.build_runtime(FakeSDK(), config, {"user_id": None, "message": "hi"})

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "agent context config:" in messages[0]
    assert "context_max_tokens=32000" in messages[0]
    assert "context_compression=summary" in messages[0]
    assert "tool_result_max_chars=16000" in messages[0]


def test_context_debug_logs_compression_events_without_forwarding(monkeypatch, caplog):
    monkeypatch.setenv("AGENT_CONTEXT_DEBUG", "1")
    forwarded = []
    event = types.SimpleNamespace(
        type=types.SimpleNamespace(value="context_compressed"),
        session_id="conv-1",
        iteration=6,
        data={
            "before_tokens": 34000,
            "after_tokens": 16000,
            "dropped_messages": 8,
            "summarized": True,
            "fallback": False,
        },
    )

    with caplog.at_level("INFO", logger="app.agent.context"):
        agent_runtime._bridge_tool_event(event, lambda kind, value=None: forwarded.append(kind))

    assert forwarded == []
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "context_compressed session=conv-1 iteration=6" in messages[0]
    assert "before_tokens=34000" in messages[0]
    assert "after_tokens=16000" in messages[0]
    assert "summarized=True" in messages[0]


def test_available_sdk_with_config_is_configured(monkeypatch):
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: FakeSDK())

    assert agent_runtime.is_locally_configured() is True
