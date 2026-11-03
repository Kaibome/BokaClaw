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


def _stream_chunk(content=None, tool_calls=None, finish_reason=None, usage=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def test_stream_complete_yields_content_and_usage(mocker):
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    chunks = [
        _stream_chunk(content="Hel"),
        _stream_chunk(content="lo"),
        _stream_chunk(finish_reason="stop", usage=usage),
    ]
    client = mocker.Mock()
    client.chat.completions.create.return_value = iter(chunks)
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    result = list(provider.stream_complete([{"role": "user", "content": "hi"}]))

    assert [chunk.content for chunk in result] == ["Hel", "lo", None]
    assert result[-1].finish_reason == "stop"
    assert result[-1].usage == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    assert client.chat.completions.create.call_args.kwargs["stream"] is True


def test_stream_complete_reassembles_tool_call_fragments(mocker):
    chunks = [
        _stream_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id="call_1",
                    function=SimpleNamespace(name="add", arguments='{"a": 1'),
                )
            ]
        ),
        _stream_chunk(
            tool_calls=[
                SimpleNamespace(
                    index=0,
                    id=None,
                    function=SimpleNamespace(name=None, arguments=', "b": 2}'),
                )
            ],
            finish_reason="tool_calls",
        ),
    ]
    client = mocker.Mock()
    client.chat.completions.create.return_value = iter(chunks)
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    result = list(provider.stream_complete([{"role": "user", "content": "add"}]))

    first_call = result[0].tool_calls[0]
    assert first_call.index == 0
    assert first_call.id == "call_1"
    assert first_call.name == "add"
    assert first_call.arguments == '{"a": 1'
    assert result[1].tool_calls[0].arguments == ', "b": 2}'
    assert result[1].finish_reason == "tool_calls"


def test_stream_errors_are_wrapped(mocker):
    client = mocker.Mock()
    client.chat.completions.create.side_effect = RuntimeError("rate limited")
    provider = OpenAICompatibleProvider(ModelConfig(model="test-model", api_key="k"), client=client)

    with pytest.raises(ModelError, match="rate limited"):
        list(provider.stream_complete([{"role": "user", "content": "go"}]))
