from __future__ import annotations

from types import SimpleNamespace

import pytest

from bokaclaw_agent.config import ModelConfig
from bokaclaw_agent.exceptions import ConfigurationError, ModelError
from bokaclaw_agent.providers.openai_compatible import OpenAICompatibleProvider


def _mock_client(mocker, response):
    client = mocker.Mock()
    client.chat.completions.create.return_value = response
    return client


def _chat_response(tool_call=None, function_call=None, content=None, finish_reason="tool_calls"):
    message = SimpleNamespace(
        content=content,
        tool_calls=[tool_call] if tool_call is not None else None,
        function_call=function_call,
    )
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(prompt_tokens=11, completion_tokens=7, total_tokens=18)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_complete_preserves_tool_call_id_and_arguments(mocker):
    tool_call = SimpleNamespace(
        id="call_abc",
        function=SimpleNamespace(name="add", arguments='{"a": 1, "b": 2}'),
    )
    client = _mock_client(mocker, _chat_response(tool_call=tool_call))
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    response = provider.complete(
        [{"role": "user", "content": "add"}],
        tools=[{"type": "function", "function": {"name": "add"}}],
        tool_choice="auto",
    )

    assert response.content is None
    assert response.finish_reason == "tool_calls"
    assert response.usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_abc"
    assert response.tool_calls[0].name == "add"
    assert response.tool_calls[0].arguments == {"a": 1, "b": 2}
    assert response.tool_calls[0].arguments_error is None

    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["tools"] == [{"type": "function", "function": {"name": "add"}}]
    assert kwargs["tool_choice"] == "auto"
    assert "temperature" not in kwargs


def test_invalid_json_arguments_are_surfaced_without_crashing(mocker):
    tool_call = SimpleNamespace(id="call_bad", function=SimpleNamespace(name="add", arguments="{oops"))
    client = _mock_client(mocker, _chat_response(tool_call=tool_call))
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    response = provider.complete([{"role": "user", "content": "add"}], tools=[{"type": "function"}])

    call = response.tool_calls[0]
    assert call.arguments == {}
    assert call.raw_arguments == "{oops"
    assert call.arguments_error is not None


def test_legacy_function_call_is_normalized(mocker):
    function_call = SimpleNamespace(name="old_tool", arguments='{"value": 1}')
    client = _mock_client(mocker, _chat_response(function_call=function_call))
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    response = provider.complete([{"role": "user", "content": "go"}])

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id.startswith("call_")
    assert response.tool_calls[0].name == "old_tool"
    assert response.tool_calls[0].arguments == {"value": 1}


def test_model_errors_are_wrapped(mocker):
    client = _mock_client(mocker, None)
    client.chat.completions.create.side_effect = RuntimeError("rate limited")
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    with pytest.raises(ModelError, match="rate limited"):
        provider.complete([{"role": "user", "content": "go"}])


def test_constructor_builds_client_from_config(mocker, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    openai_cls = mocker.patch("bokaclaw_agent.providers.openai_compatible.OpenAI")

    OpenAICompatibleProvider(
        ModelConfig(
            model="test-model",
            base_url="https://example.test/v1",
            timeout=12.5,
            max_retries=4,
        )
    )

    openai_cls.assert_called_once_with(
        api_key="env-key",
        base_url="https://example.test/v1",
        timeout=12.5,
        max_retries=4,
        default_headers=None,
    )


def test_missing_api_key_raises_configuration_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="API key"):
        OpenAICompatibleProvider(ModelConfig(model="test-model"))
