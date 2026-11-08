"""Agent memory tool tests (no Postgres, no model provider)."""

from types import SimpleNamespace

import app.agent.runtime as runtime
import app.core.server_state as server_state


class FakeSDK:
    @staticmethod
    def tool(fn, effect=None):
        return fn


class FakeService:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(require_approval=False)
        self.rows = {
            "m1": {"id": "m1", "user_id": "u1", "memory": "用户喜欢黑咖啡"},
            "other": {"id": "other", "user_id": "u2", "memory": "别人的记忆"},
        }
        self.saved = None

    def search(self, query, filters=None, top_k=20):
        return {"results": [self.rows["m1"]]}

    def get(self, memory_id):
        return self.rows.get(memory_id)

    def add(self, messages, user_id, memory_type=None, metadata=None):
        self.saved = {
            "content": messages[0]["content"],
            "user_id": user_id,
            "kind": memory_type,
            "metadata": metadata,
        }
        return {"results": [{"id": "m2", "memory": messages[0]["content"], "event": "ADD"}]}

    def update(self, memory_id, text=None, metadata=None):
        return {"message": "Memory updated successfully!"}

    def delete(self, memory_id):
        return {"message": "Memory deleted successfully"}


def _tools(service: FakeService, monkeypatch) -> list:
    monkeypatch.setattr(server_state, "get_memory_instance", lambda: service)
    return runtime._memory_tools(FakeSDK, "u1")


def test_tool_names(monkeypatch) -> None:
    tools = _tools(FakeService(), monkeypatch)
    assert [tool.__name__ for tool in tools] == [
        "search_memories",
        "get_memory",
        "save_memory",
        "update_memory",
        "delete_memory",
    ]


def test_search_and_get(monkeypatch) -> None:
    tools = _tools(FakeService(), monkeypatch)
    search, get, *_ = tools
    assert search("咖啡")["results"][0]["id"] == "m1"
    assert get("m1")["memory"] == "用户喜欢黑咖啡"
    assert get("other") == {"error": "Memory not found."}


def test_save_writes_curated_metadata(monkeypatch) -> None:
    service = FakeService()
    tools = _tools(service, monkeypatch)
    save = tools[2]
    assert save("用户喜欢拿铁", "preference", 0.9) == {"status": "saved"}
    assert service.saved["kind"] == "preference"
    assert service.saved["metadata"]["source"] == "curated"
    assert service.saved["metadata"]["importance"] == 0.9


def test_update_and_delete_reject_other_users(monkeypatch) -> None:
    tools = _tools(FakeService(), monkeypatch)
    update, delete = tools[3], tools[4]
    assert update("other", "x") == {"error": "Memory not found."}
    assert delete("other") == {"error": "Memory not found."}
    assert update("m1", "用户喜欢拿铁") == {"status": "updated"}
    assert delete("m1") == {"status": "deleted"}
