"""Small LLM adapter for setup helpers such as ``/generate-instructions``."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from .config import MemorySettings
from .errors import MemoryUnavailable, MemoryValidationError

logger = logging.getLogger(__name__)


def _meter_response(usage: Any) -> None:
    try:
        from app.agent.usage import CURRENT_METER

        meter = CURRENT_METER.get()
        if meter is not None:
            meter.record_response("memory_llm", usage)
    except Exception:  # pragma: no cover
        logger.debug("Failed to record memory LLM usage", exc_info=True)


def _meter_unknown() -> None:
    try:
        from app.agent.usage import CURRENT_METER

        meter = CURRENT_METER.get()
        if meter is not None:
            meter.record("memory_llm", None)
    except Exception:  # pragma: no cover
        logger.debug("Failed to record an unknown memory LLM call", exc_info=True)


def _api_key(provider: str, config: Dict[str, Any]) -> str | None:
    explicit = config.get("api_key")
    if explicit:
        return str(explicit)
    env_names = {
        "openai": ("OPENAI_API_KEY",),
        "deepseek": ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    }
    for name in env_names.get(provider, ()):
        value = os.getenv(name)
        if value:
            return value
    return None


def generate_response(settings: MemorySettings, messages: List[Dict[str, str]]) -> str:
    provider = (settings.llm_provider or "openai").strip().lower()
    model = settings.llm_model or "gpt-5-mini"
    config = settings.llm_config or {}
    api_key = _api_key(provider, config)

    if provider in {"openai", "deepseek"}:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise MemoryUnavailable("openai is not installed") from exc
        base_url = config.get("base_url") or config.get("openai_base_url")
        if provider == "deepseek" and not base_url:
            base_url = "https://api.deepseek.com"
        if not api_key and not base_url:
            raise MemoryValidationError("an API key or base URL is required for the configured LLM")
        client = OpenAI(api_key=api_key or "local", base_url=base_url)
        try:
            response = client.chat.completions.create(model=model, messages=messages, temperature=0.2)
        except Exception as exc:
            raise MemoryUnavailable(f"LLM request failed: {exc}") from exc
        _meter_response(getattr(response, "usage", None))
        choice = response.choices[0].message if response.choices else None
        return str(getattr(choice, "content", "") or "")

    if provider == "anthropic":
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise MemoryUnavailable("anthropic is not installed") from exc
        if not api_key:
            raise MemoryValidationError("an Anthropic API key is required")
        client = anthropic.Anthropic(api_key=api_key)
        system = "\n".join(item["content"] for item in messages if item.get("role") == "system")
        user = "\n".join(item["content"] for item in messages if item.get("role") != "system")
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1024,
                system=system or anthropic.NOT_GIVEN,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:
            raise MemoryUnavailable(f"Anthropic request failed: {exc}") from exc
        _meter_response(getattr(response, "usage", None))
        blocks = getattr(response, "content", []) or []
        return "".join(str(getattr(block, "text", "") or "") for block in blocks)

    if provider == "gemini":
        try:
            import google.generativeai as genai
        except ImportError as exc:  # pragma: no cover
            raise MemoryUnavailable("google-generativeai is not installed") from exc
        if not api_key:
            raise MemoryValidationError("a Gemini API key is required")
        genai.configure(api_key=api_key)
        prompt = "\n".join(item["content"] for item in messages)
        try:
            response = genai.GenerativeModel(model).generate_content(prompt)
        except Exception as exc:
            raise MemoryUnavailable(f"Gemini request failed: {exc}") from exc
        _meter_unknown()
        return str(getattr(response, "text", "") or "")

    raise MemoryValidationError(f"unsupported LLM provider: {provider}")
