"""Token counting for context budgets.

Counts come from a real tokenizer whenever one can be loaded. :class:`TiktokenTokenCounter`
resolves the encoding for a model name (falling back to ``o200k_base`` for models
tiktoken does not know, such as most self-hosted OpenAI-compatible servers) and
caches it for the life of the process.

If tiktoken is not installed, or its encoding data cannot be loaded (for example
an offline host on first use), counting degrades to a conservative character
estimate instead of failing the run. ``token_counter_override`` exists for tests
and for hosts that provide their own tokenizer.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_ENCODING = "o200k_base"


@runtime_checkable
class TokenCounter(Protocol):
    """Anything that can report how many model tokens a piece of text uses."""

    def count(self, text: str | None) -> int:
        """Return the token count of ``text`` (``0`` when it is empty)."""


class HeuristicTokenCounter:
    """Conservative character-based fallback used when a tokenizer is unavailable.

    ASCII text is counted as roughly 0.4 tokens per character; non-ASCII (for
    example CJK) is counted as one token per character. It over-counts most
    models on purpose, so a budget derived from it stays on the safe side.
    """

    def count(self, text: str | None) -> int:
        if not text:
            return 0
        ascii_chars = sum(1 for char in text if ord(char) < 128)
        return math.ceil(ascii_chars * 0.4 + (len(text) - ascii_chars))


class TiktokenTokenCounter:
    """Count tokens with tiktoken's BPE encodings.

    ``encoding_name`` wins when given; otherwise a known ``model`` maps to its
    exact encoding, and anything else uses :data:`DEFAULT_ENCODING`. The
    encoding is loaded lazily on the first count and reused afterwards, because
    loading can be expensive and, on first use, may need the network.
    """

    def __init__(self, *, model: str | None = None, encoding_name: str | None = None) -> None:
        self.model = model
        self.encoding_name = encoding_name
        self._encoding: Any | None = None
        self._resolved = False
        self._fallback = HeuristicTokenCounter()
        self._lock = threading.Lock()
        self._warned = False

    def count(self, text: str | None) -> int:
        if not text:
            return 0
        encoding = self._get_encoding()
        if encoding is None:
            return self._fallback.count(text)
        try:
            # ``disallowed_special=()`` keeps literal special-token strings
            # (for example "<|endoftext|>" inside tool output) countable.
            return len(encoding.encode(text, disallowed_special=()))
        except Exception as exc:  # noqa: BLE001 - counting must never fail a run
            self._warn_once(f"tokenizer failed to encode text: {exc}; falling back to character estimates")
            return self._fallback.count(text)

    def _get_encoding(self) -> Any | None:
        if self._resolved:
            return self._encoding
        with self._lock:
            if self._resolved:
                return self._encoding
            self._resolved = True
            try:
                import tiktoken
            except ImportError:
                self._warn_once("tiktoken is not installed; context budgets use conservative character estimates")
                return None
            try:
                if self.encoding_name:
                    self._encoding = tiktoken.get_encoding(self.encoding_name)
                elif self.model:
                    try:
                        self._encoding = tiktoken.encoding_for_model(self.model)
                    except KeyError:
                        # Most OpenAI-compatible servers (DeepSeek, Qwen, vLLM,
                        # Ollama) are not in tiktoken's model table.
                        self._encoding = tiktoken.get_encoding(DEFAULT_ENCODING)
                else:
                    self._encoding = tiktoken.get_encoding(DEFAULT_ENCODING)
            except Exception as exc:  # noqa: BLE001 - offline first use, bad encoding name
                self._warn_once(f"could not load tokenizer encoding ({exc}); falling back to character estimates")
                self._encoding = None
            return self._encoding

    def _warn_once(self, message: str) -> None:
        if not self._warned:
            self._warned = True
            logger.warning(message)


_counters: dict[str, TokenCounter] = {}
_override: TokenCounter | None = None
_counters_lock = threading.Lock()


def get_token_counter(model: str | None = None) -> TokenCounter:
    """Return the process-wide counter for ``model``.

    One counter is cached per model name because the encoding it resolves to is
    part of its behaviour. A counter installed with :func:`token_counter_override`
    wins over the cache.
    """

    if _override is not None:
        return _override
    key = model or ""
    with _counters_lock:
        counter = _counters.get(key)
        if counter is None:
            counter = TiktokenTokenCounter(model=model)
            _counters[key] = counter
        return counter


@contextmanager
def token_counter_override(counter: TokenCounter | None) -> Iterator[None]:
    """Temporarily replace the default counter (``None`` restores the cache).

    Intended for tests, which must stay offline, and for hosts that count tokens
    with their own tokenizer.
    """

    global _override
    previous = _override
    _override = counter
    try:
        yield
    finally:
        _override = previous


def _reset_token_counters() -> None:
    """Drop cached counters and any override. Used by tests."""

    global _override
    with _counters_lock:
        _counters.clear()
    _override = None
