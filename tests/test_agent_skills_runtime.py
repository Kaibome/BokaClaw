"""Runtime integration: catalog injection and the ``Skill`` tool registration."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import app.agent.runtime as agent_runtime  # noqa: E402
from app.agent.skills import prepare_skill_context, reset_skill_catalog_cache  # noqa: E402
from app.agent.skills.config import SkillsConfig  # noqa: E402


class FakeTool:
    def __init__(self, fn, name=None, effect=None):
        self.fn = fn
        self.name = name or fn.__name__
        self.effect = effect


class FakeRuntime:
    def __init__(self, config, tools, on_event=None, **kwargs):
        self.config = config
        self.tools = list(tools)
        self.on_event = on_event
        self.stream_calls = []
        self.closed = False

    def stream(self, messages, **kwargs):
        self.stream_calls.append((messages, kwargs))
        yield "ok"
        return types.SimpleNamespace(content="ok", usage={}, stop_reason="final")

    def close(self):
        self.closed = True


class FakeSDK:
    class ModelConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class AgentConfig:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def __init__(self):
        self.runtimes = []
        self.tools = []

    def tool(self, fn, *, name=None, effect=None, concurrency_safe=None):
        tool = FakeTool(fn, name=name, effect=effect)
        self.tools.append(tool)
        return tool

    def AgentRuntime(self, config, *, tools=(), on_event=None, **kwargs):
        runtime = FakeRuntime(config, tools, on_event=on_event)
        self.runtimes.append(runtime)
        return runtime


def _write_skill(root: Path, name: str = "demo") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A demo skill for runtime wiring.\n---\n# Instructions\nDo it.\n",
        encoding="utf-8",
    )
    return directory


def _config():
    return agent_runtime.LocalAgentConfig(
        model="test-model",
        api_key="test-key",
        base_url=None,
        temperature=0.2,
        max_tokens=None,
        request_timeout=60.0,
        max_retries=2,
        system_prompt="system",
        max_iterations=5,
        max_seconds=5.0,
        memory_recall=False,
        research_memory=False,
        subagents_enabled=False,
    )


def _payload():
    return {
        "conversation_id": "conv-1",
        "message": "hello",
        "history": [{"role": "assistant", "content": "hi"}],
        "user_id": None,
        "run_id": "run-1",
    }


async def _collect():
    return [event async for event in agent_runtime.stream_local_agent(_payload())]


@pytest.fixture(autouse=True)
def _clean_skills_env(monkeypatch):
    for name in (
        "AGENT_SKILLS_ENABLED",
        "AGENT_SKILLS_ROOT",
        "AGENT_SKILLS_USER_ROOT",
        "AGENT_SKILLS_WORKSPACE_DIR",
        "AGENT_SKILLS_CATALOG_TTL_SECONDS",
        "AGENT_SKILLS_ALLOW_SCRIPTS",
        "AGENT_DEV_TOOLS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_DEV_TOOLS", "false")
    reset_skill_catalog_cache()


def test_prepare_skill_context_returns_tools_and_block(tmp_path: Path):
    _write_skill(tmp_path)
    sdk = FakeSDK()
    config = SkillsConfig(global_root=tmp_path, catalog_ttl_seconds=0)

    context = prepare_skill_context(sdk, None, config=config)

    assert [tool.name for tool in context.tools] == ["Skill"]
    assert context.block is not None
    assert "<available-skills>" in context.block
    assert "demo" in context.block
    assert context.catalog is not None
    assert context.catalog.get("demo") is not None


def test_prepare_skill_context_adds_skill_run_when_scripts_are_allowed(tmp_path: Path):
    _write_skill(tmp_path)
    sdk = FakeSDK()
    config = SkillsConfig(global_root=tmp_path, catalog_ttl_seconds=0, allow_scripts=True)

    context = prepare_skill_context(sdk, None, config=config)

    assert [tool.name for tool in context.tools] == ["Skill", "SkillRun"]


def test_prepare_skill_context_is_empty_without_skills(tmp_path: Path):
    sdk = FakeSDK()
    config = SkillsConfig(global_root=tmp_path, catalog_ttl_seconds=0)

    context = prepare_skill_context(sdk, None, config=config)

    assert context.tools == []
    assert context.block is None
    assert context.catalog is not None
    assert context.catalog.skills == ()


@pytest.mark.asyncio
async def test_stream_local_agent_injects_catalog_and_registers_skill(monkeypatch, tmp_path: Path):
    _write_skill(tmp_path)
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_SKILLS_CATALOG_TTL_SECONDS", "0")
    sdk = FakeSDK()
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)
    monkeypatch.setattr(agent_runtime, "load_mcp_config", lambda: None)

    await _collect()

    runtime = sdk.runtimes[0]
    messages = runtime.stream_calls[0][0]
    assert messages[-1] == {"role": "user", "content": "hello"}
    assert "<available-skills>" in messages[-2]["content"]
    assert "demo" in messages[-2]["content"]
    assert "Skill" in [tool.name for tool in runtime.tools]


@pytest.mark.asyncio
async def test_stream_local_agent_is_unchanged_when_skills_are_disabled(monkeypatch, tmp_path: Path):
    _write_skill(tmp_path)
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "false")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(tmp_path))
    sdk = FakeSDK()
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)
    monkeypatch.setattr(agent_runtime, "load_mcp_config", lambda: None)

    await _collect()

    runtime = sdk.runtimes[0]
    messages = runtime.stream_calls[0][0]
    assert messages[-1] == {"role": "user", "content": "hello"}
    assert all("<available-skills>" not in str(message.get("content")) for message in messages)
    assert "Skill" not in [tool.name for tool in runtime.tools]


@pytest.mark.asyncio
async def test_a_broken_skill_root_does_not_fail_the_run(monkeypatch, tmp_path: Path):
    broken = tmp_path / "not-a-directory"
    broken.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AGENT_SKILLS_ENABLED", "true")
    monkeypatch.setenv("AGENT_SKILLS_ROOT", str(broken))
    monkeypatch.setenv("AGENT_SKILLS_CATALOG_TTL_SECONDS", "0")
    sdk = FakeSDK()
    monkeypatch.setattr(agent_runtime, "load_local_config", lambda: _config())
    monkeypatch.setattr(agent_runtime, "_load_sdk", lambda: sdk)
    monkeypatch.setattr(agent_runtime, "load_mcp_config", lambda: None)

    events = await _collect()

    assert events
    assert sdk.runtimes[0].stream_calls
