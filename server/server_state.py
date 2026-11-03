import json
import logging
import threading
from copy import deepcopy
from typing import Any, Callable, Dict

from mem0 import Memory

_state_lock = threading.RLock()
_current_config: Dict[str, Any] = {}
_memory_instance: Memory | None = None
_session_factory: Callable | None = None

# pgvector cannot change a collection's vector width after creation, so the
# width has to follow the embedder. Only models whose width is known are
# listed; anything else keeps whatever the operator configured explicitly.
_EMBEDDING_DIMS: Dict[tuple, int] = {
    ("openai", "text-embedding-ada-002"): 1536,
    ("openai", "text-embedding-3-small"): 1536,
    ("openai", "text-embedding-3-large"): 3072,
    ("gemini", "models/gemini-embedding-001"): 3072,
    ("gemini", "gemini-embedding-001"): 3072,
    ("fastembed", "BAAI/bge-small-zh-v1.5"): 512,
}


def _embedding_dims(provider: str, model: str) -> int | None:
    dims = _EMBEDDING_DIMS.get((provider, model))
    if dims is not None or provider != "fastembed" or not model:
        return dims

    try:
        from fastembed import TextEmbedding

        for entry in TextEmbedding.list_supported_models():
            if entry.get("model") == model:
                return entry.get("dim")
    except Exception:
        logging.warning("Could not resolve embedding dimensions for %s", model, exc_info=True)
    return None


def _sync_embedding_dims(config: Dict[str, Any]) -> None:
    embedder = config.get("embedder") or {}
    provider = embedder.get("provider")
    model = (embedder.get("config") or {}).get("model")
    dims = _embedding_dims(provider, model)
    if not dims:
        return

    vector_store = config.setdefault("vector_store", {})
    vector_config = vector_store.setdefault("config", {})
    vector_config["embedding_model_dims"] = dims


def set_session_factory(factory: Callable) -> None:
    global _session_factory
    _session_factory = factory


def _load_overrides() -> Dict[str, Any]:
    try:
        if _session_factory is None:
            return {}
        from models import Settings

        session = _session_factory()
        try:
            row = session.get(Settings, "config_overrides")
            if row is None:
                return {}
            return json.loads(row.value)
        finally:
            session.close()
    except Exception:
        return {}


def _save_overrides(overrides: Dict[str, Any]) -> None:
    try:
        if _session_factory is None:
            return
        from models import Settings
        from sqlalchemy.dialects.postgresql import insert

        session = _session_factory()
        try:
            serialized = json.dumps(overrides)
            stmt = (
                insert(Settings)
                .values(key="config_overrides", value=serialized)
                .on_conflict_do_update(
                    index_elements=[Settings.key],
                    set_={"value": serialized},
                )
            )
            session.execute(stmt)
            session.commit()
        finally:
            session.close()
    except Exception:
        logging.warning("Failed to persist config overrides to database", exc_info=True)


def _merge_config(base: Dict[str, Any], updates: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)

    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value

    return merged


def initialize_state(default_config: Dict[str, Any]) -> None:
    global _current_config, _memory_instance
    with _state_lock:
        _current_config = deepcopy(default_config)
        overrides = _load_overrides()
        if overrides:
            _current_config = _merge_config(_current_config, overrides)
        _sync_embedding_dims(_current_config)
        _memory_instance = Memory.from_config(_current_config)


def update_config(updates: Dict[str, Any]) -> Dict[str, Any]:
    global _current_config, _memory_instance
    with _state_lock:
        next_config = _merge_config(_current_config, updates)
        _sync_embedding_dims(next_config)
        _current_config = next_config
        _memory_instance = Memory.from_config(next_config)
        overrides = _load_overrides()
        overrides = _merge_config(overrides, updates)
        _save_overrides(overrides)
        return deepcopy(_current_config)


def get_current_config() -> Dict[str, Any]:
    with _state_lock:
        return deepcopy(_current_config)


def get_memory_instance() -> Memory:
    with _state_lock:
        if _memory_instance is None:
            raise RuntimeError("Mem0 runtime has not been initialized.")
        return _memory_instance
