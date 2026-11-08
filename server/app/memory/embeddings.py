"""Embedding adapters with metering and fixed-width storage vectors."""

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Protocol

from .config import STORAGE_DIMS, MemorySettings
from .errors import MemoryUnavailable, MemoryValidationError

logger = logging.getLogger(__name__)

_KNOWN_DIMS: Dict[tuple[str, str], int] = {
    ("openai", "text-embedding-ada-002"): 1536,
    ("openai", "text-embedding-3-small"): 1536,
    ("openai", "text-embedding-3-large"): 3072,
    ("gemini", "models/gemini-embedding-001"): 3072,
    ("gemini", "gemini-embedding-001"): 3072,
    ("fastembed", "BAAI/bge-small-zh-v1.5"): 512,
}


class Embedder(Protocol):
    model_id: str
    dims: int

    def embed_documents(self, texts: List[str]) -> List[List[float]]: ...

    def embed_query(self, text: str) -> List[float]: ...


def _meter_response(usage: Any) -> None:
    try:
        from app.agent.usage import CURRENT_METER

        meter = CURRENT_METER.get()
        if meter is not None:
            meter.record_response("memory_embedding", usage)
    except Exception:  # pragma: no cover - instrumentation must not break a turn
        logger.debug("Failed to record embedding usage", exc_info=True)


def _meter_unknown(calls: int = 1) -> None:
    try:
        from app.agent.usage import CURRENT_METER

        meter = CURRENT_METER.get()
        if meter is not None:
            meter.record("memory_embedding", None, calls=max(1, calls))
    except Exception:  # pragma: no cover
        logger.debug("Failed to record an unknown embedding call", exc_info=True)


def normalize_vector(values: List[float], dims: int = STORAGE_DIMS) -> List[float]:
    vector = [float(value) for value in values]
    if len(vector) > dims:
        raise MemoryValidationError(f"embedding has {len(vector)} dimensions, storage supports {dims}")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm > 0:
        vector = [value / norm for value in vector]
    if len(vector) < dims:
        vector.extend([0.0] * (dims - len(vector)))
    return vector


def vector_literal(values: List[float]) -> str:
    return "[" + ",".join(f"{value:.7f}" for value in values) + "]"


class _BaseEmbedder:
    model_id: str
    dims: int

    def embed_query(self, text: str) -> List[float]:
        return self.embed_documents([text])[0]


class FastEmbedder(_BaseEmbedder):
    def __init__(self, model: str, config: Dict[str, Any]) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise MemoryUnavailable("fastembed is not installed") from exc

        self.model_id = f"fastembed:{model}"
        self.dims = _KNOWN_DIMS.get(("fastembed", model)) or self._probe_dims(TextEmbedding, model)
        cache_dir = config.get("cache_dir") or os.getenv("FASTEMBED_CACHE_PATH")
        kwargs: Dict[str, Any] = {"model_name": model}
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        try:
            self._model = TextEmbedding(**kwargs)
        except Exception as exc:  # pragma: no cover - provider/network failure
            raise MemoryUnavailable(f"could not initialize fastembed model {model}: {exc}") from exc

    @staticmethod
    def _probe_dims(text_embedding: Any, model: str) -> int:
        try:
            for entry in text_embedding.list_supported_models():
                if entry.get("model") == model and entry.get("dim"):
                    return int(entry["dim"])
        except Exception:  # pragma: no cover
            logger.debug("Could not resolve fastembed dimensions for %s", model, exc_info=True)
        return 512

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        cleaned = [text.replace("\n", " ") for text in texts]
        try:
            vectors = list(self._model.embed(cleaned))
        except Exception as exc:
            raise MemoryUnavailable(f"fastembed failed: {exc}") from exc
        _meter_unknown(len(cleaned))
        return [normalize_vector([float(value) for value in vector], STORAGE_DIMS) for vector in vectors]


class OpenAIEmbedder(_BaseEmbedder):
    def __init__(self, model: str, config: Dict[str, Any]) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise MemoryUnavailable("openai is not installed") from exc

        api_key = config.get("api_key") or os.getenv("OPENAI_API_KEY")
        base_url = config.get("base_url") or config.get("openai_base_url")
        if not api_key and not base_url:
            raise MemoryValidationError("an OpenAI API key or base URL is required for embeddings")
        self.model_id = f"openai:{model}"
        self.dims = _KNOWN_DIMS.get(("openai", model), int(config.get("dimensions") or 1536))
        self._model_name = model
        self._client = OpenAI(api_key=api_key or "local", base_url=base_url)

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(
                model=self._model_name,
                input=[text.replace("\n", " ") for text in texts],
            )
        except Exception as exc:
            raise MemoryUnavailable(f"embedding request failed: {exc}") from exc
        _meter_response(getattr(response, "usage", None))
        data = sorted(response.data, key=lambda item: getattr(item, "index", 0))
        return [normalize_vector(list(item.embedding), STORAGE_DIMS) for item in data]


class GeminiEmbedder(_BaseEmbedder):
    def __init__(self, model: str, config: Dict[str, Any]) -> None:
        try:
            import google.generativeai as genai
        except ImportError as exc:  # pragma: no cover
            raise MemoryUnavailable("google-generativeai is not installed") from exc

        api_key = config.get("api_key") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise MemoryValidationError("a Gemini API key is required for embeddings")
        genai.configure(api_key=api_key)
        self._genai = genai
        self._model_name = model
        self.model_id = f"gemini:{model}"
        self.dims = _KNOWN_DIMS.get(("gemini", model), int(config.get("dimensions") or 3072))

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for text in texts:
            try:
                response = self._genai.embed_content(
                    model=self._model_name,
                    content=text.replace("\n", " "),
                    task_type="retrieval_document",
                )
            except Exception as exc:
                raise MemoryUnavailable(f"Gemini embedding failed: {exc}") from exc
            _meter_unknown()
            vectors.append(normalize_vector(list(response["embedding"]), STORAGE_DIMS))
        return vectors


def create_embedder(settings: MemorySettings) -> Embedder:
    provider = (settings.embedder_provider or "openai").strip().lower()
    model = (settings.embedder_model or "text-embedding-3-small").strip()
    config = settings.embedder_config or {}
    if provider in {"openai", "deepseek"}:
        return OpenAIEmbedder(model, config)
    if provider == "fastembed":
        return FastEmbedder(model, config)
    if provider == "gemini":
        return GeminiEmbedder(model, config)
    raise MemoryValidationError(f"unsupported embedder provider: {provider}")
