"""Service tests with a fake index repository (no Postgres)."""

from pathlib import Path

from app.memory.capture import capture_turn
from app.memory.config import MemorySettings
from app.memory.paths import user_root
from app.memory.retrieval import recall_for_turn
from app.memory.service import WorkspaceMemory


class FakeEmbedder:
    model_id = "fake:test"
    dims = 4

    def embed_documents(self, texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def embed_query(self, text):
        return [0.1, 0.2, 0.3, 0.4]


class FakeRepository:
    def __init__(self) -> None:
        self.rows = {}
        self.history = []
        self.query_rows = []

    def get_entry(self, memory_id, show_expired=False):
        return self.rows.get(memory_id)

    def upsert_entries(self, entries):
        for entry in entries:
            self.rows[entry.id] = {
                "id": entry.id,
                "user_id": entry.user_id,
                "memory": entry.content,
                "kind": entry.kind,
                "source": entry.source,
                "trust": entry.trust,
                "importance": entry.importance,
                "score": 0.9,
            }

    def record_history(self, memory_id, user_id, event, old_memory, new_memory, **kw):
        self.history.append((memory_id, user_id, event, old_memory, new_memory))

    def get_all(self, filters, top_k=20, show_expired=False):
        return list(self.rows.values())[:top_k]

    def search(self, query, **kwargs):
        return list(self.query_rows)

    def delete_entries(self, ids):
        for memory_id in ids:
            self.rows.pop(memory_id, None)
        return len(list(ids))

    def delete_user(self, user_id):
        return 0

    def clear_all(self):
        self.rows.clear()


def _service(tmp_path) -> WorkspaceMemory:
    settings = MemorySettings(root=tmp_path, embedder_provider="openai")
    service = WorkspaceMemory(settings, session_factory=lambda: None, embedder=FakeEmbedder())
    service.repository = FakeRepository()
    return service


def test_add_get_update_delete(tmp_path) -> None:
    service = _service(tmp_path)
    response = service.add([{"role": "user", "content": "用户喜欢黑咖啡"}], user_id="u1")
    memory_id = response["results"][0]["id"]
    assert (user_root(service.settings, "u1") / "MEMORY.md").exists()

    item = service.get(memory_id)
    assert item["memory"] == "用户喜欢黑咖啡"

    service.update(memory_id, text="用户喜欢拿铁")
    assert service.get(memory_id)["memory"] == "用户喜欢拿铁"

    service.delete(memory_id)
    assert service.get(memory_id) is None


def test_identity_writes_user_file(tmp_path) -> None:
    service = _service(tmp_path)
    service.add([{"role": "user", "content": "用户住在深圳"}], user_id="u1", memory_type="identity")
    assert (user_root(service.settings, "u1") / "USER.md").exists()


def test_capture_skips_short_and_writes_episode(tmp_path) -> None:
    service = _service(tmp_path)
    short = capture_turn(service, "u1", "ok", "收到")
    assert short.reason == "short_or_noise"

    report = capture_turn(
        service,
        "u1",
        "帮我把 agent_research 目录整理一下，保留 raw、wiki 和 meta 三个子目录。",
        "已整理，保留 raw/wiki/meta 三个子目录。",
    )
    assert report.action == "appended"
    assert report.path is not None
    assert Path(report.path).exists()


def test_capture_skips_duplicate(tmp_path) -> None:
    service = _service(tmp_path)
    message = "帮我把 agent_research 目录整理一下，保留 raw、wiki 和 meta 三个子目录。"
    first = capture_turn(service, "u1", message, "已整理。")
    second = capture_turn(service, "u1", message, "已整理。")
    assert first.action == "appended"
    assert second.action == "skipped"
    assert second.reason == "duplicate"


def test_recall_combines_profile_and_query(tmp_path) -> None:
    service = _service(tmp_path)
    service.repository.rows["m-profile"] = {
        "id": "m-profile",
        "user_id": "u1",
        "memory": "用户使用中文交流",
        "kind": "identity",
        "source": "curated",
        "trust": "owner",
        "importance": 0.95,
    }
    service.repository.query_rows = [
        {
            "id": "m-query",
            "user_id": "u1",
            "memory": "用户在深圳工作",
            "kind": "project",
            "source": "capture",
            "trust": "owner",
            "importance": 0.75,
            "score": 0.8,
        }
    ]
    result = recall_for_turn(service, "u1", "我在哪里工作？")
    assert "用户使用中文交流" in result.block
    assert "用户在深圳工作" in result.block
    assert result.profile_chars > 0
    assert result.query_hits == 1


def test_recall_does_not_raise_on_backend_failure(tmp_path) -> None:
    service = _service(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("index down")

    service.repository.get_all = boom  # type: ignore[assignment]
    result = recall_for_turn(service, "u1", "问题")
    assert result.block == ""
    assert result.error is not None
