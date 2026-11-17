from __future__ import annotations

import sys
import types

from bokaclaw_agent.agent.tokenization import (
    DEFAULT_ENCODING,
    HeuristicTokenCounter,
    TiktokenTokenCounter,
    _reset_token_counters,
    get_token_counter,
    token_counter_override,
)


class FakeEncoding:
    """Deterministic encoding: one token per character."""

    def __init__(self, name: str) -> None:
        self.name = name

    def encode(self, text: str, *, disallowed_special: object = "all") -> list[int]:
        # The runtime must allow literal special-token text (tool output often
        # contains strings like "<|endoftext|>"); encoding it is an error.
        assert disallowed_special == ()
        return [ord(char) for char in text]


def _install_fake_tiktoken(monkeypatch, *, known_models: dict[str, str] | None = None) -> dict[str, list[str]]:
    known_models = known_models or {"gpt-4o-mini": "o200k_base"}
    calls: dict[str, list[str]] = {"encoding_for_model": [], "get_encoding": []}

    def encoding_for_model(model: str) -> FakeEncoding:
        calls["encoding_for_model"].append(model)
        try:
            return FakeEncoding(known_models[model])
        except KeyError:
            raise KeyError(model) from None

    def get_encoding(name: str) -> FakeEncoding:
        calls["get_encoding"].append(name)
        return FakeEncoding(name)

    module = types.SimpleNamespace(encoding_for_model=encoding_for_model, get_encoding=get_encoding)
    monkeypatch.setitem(sys.modules, "tiktoken", module)
    return calls


def test_heuristic_counter_counts_ascii_and_non_ascii_conservatively():
    counter = HeuristicTokenCounter()

    assert counter.count(None) == 0
    assert counter.count("") == 0
    assert counter.count("abcd") == 2
    assert counter.count("你好") == 2
    assert counter.count("abcd你好") == 4


def test_tiktoken_counter_resolves_the_model_encoding_once(monkeypatch):
    calls = _install_fake_tiktoken(monkeypatch)
    counter = TiktokenTokenCounter(model="gpt-4o-mini")

    assert counter.count("abcd") == 4
    assert counter.count("你好") == 2

    assert calls["encoding_for_model"] == ["gpt-4o-mini"]
    assert calls["get_encoding"] == []


def test_tiktoken_counter_uses_the_generic_encoding_for_unknown_models(monkeypatch):
    calls = _install_fake_tiktoken(monkeypatch)
    counter = TiktokenTokenCounter(model="deepseek-chat")

    assert counter.count("abcd") == 4

    assert calls["encoding_for_model"] == ["deepseek-chat"]
    assert calls["get_encoding"] == [DEFAULT_ENCODING]


def test_tiktoken_counter_honours_an_explicit_encoding(monkeypatch):
    calls = _install_fake_tiktoken(monkeypatch)
    counter = TiktokenTokenCounter(model="gpt-4o-mini", encoding_name="cl100k_base")

    assert counter.count("abc") == 3

    assert calls["encoding_for_model"] == []
    assert calls["get_encoding"] == ["cl100k_base"]


def test_tiktoken_counter_counts_literal_special_token_text(monkeypatch):
    _install_fake_tiktoken(monkeypatch)
    counter = TiktokenTokenCounter()

    assert counter.count("<|endoftext|>") == len("<|endoftext|>")


def test_tiktoken_counter_falls_back_when_tiktoken_is_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    counter = TiktokenTokenCounter(model="gpt-4o-mini")

    assert counter.count("abcd") == 2


def test_tiktoken_counter_falls_back_when_the_encoding_cannot_load(monkeypatch):
    def get_encoding(name: str) -> FakeEncoding:
        raise OSError(f"offline: cannot download {name}")

    monkeypatch.setitem(
        sys.modules,
        "tiktoken",
        types.SimpleNamespace(encoding_for_model=lambda model: FakeEncoding(model), get_encoding=get_encoding),
    )
    counter = TiktokenTokenCounter()

    assert counter.count("abcd") == 2
    assert counter.count("你好") == 2


def test_default_counter_is_a_tiktoken_counter():
    with token_counter_override(None):
        _reset_token_counters()
        # Constructing the counter does not load the encoding (or the network).
        assert isinstance(get_token_counter(model="gpt-4o-mini"), TiktokenTokenCounter)
        _reset_token_counters()


def test_token_counter_override_restores_the_previous_counter():
    original = get_token_counter()
    custom = HeuristicTokenCounter()

    with token_counter_override(custom):
        assert get_token_counter() is custom

    assert get_token_counter() is original


def test_get_token_counter_caches_one_counter_per_model(monkeypatch):
    _reset_token_counters()
    _install_fake_tiktoken(monkeypatch)

    first = get_token_counter(model="gpt-4o-mini")
    again = get_token_counter(model="gpt-4o-mini")
    other = get_token_counter(model="deepseek-chat")

    assert first is again
    assert first is not other
    _reset_token_counters()
