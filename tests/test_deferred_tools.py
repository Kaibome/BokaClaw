"""Unit tests for the deferred tool catalog and ToolSearch ranking."""

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app.agent.deferred_tools import (  # noqa: E402
    collect_deferred,
    render_deferred_catalog,
    search_deferred,
    tool_search_result,
)


class FakeTool:
    def __init__(self, name, description):
        self.name = name
        self.description = description

    def to_openai_spec(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }


class FakeRegistry:
    def __init__(self, names=()):
        self.active = set(names)

    @property
    def active_names(self):
        return frozenset(self.active)

    def activate(self, names):
        activated = [name for name in names if name not in self.active]
        self.active.update(activated)
        return activated

    def deactivate(self, names):
        deactivated = [name for name in names if name in self.active]
        self.active.difference_update(deactivated)
        return deactivated


def _tools():
    return [
        FakeTool("Read", "Read a file."),
        FakeTool("PptxBuild", "Build a deck."),
        FakeTool("PptxUpdateSlide", "Update one slide in an existing PowerPoint deck."),
        FakeTool("mcp__github__list_pull_requests", "List GitHub pull requests for a repository."),
        FakeTool("mcp__github__search_code", "Search GitHub code across repositories."),
        FakeTool("mcp__12306-train__query_train_tickets", "Query China Railway train tickets."),
    ]


def test_collect_deferred_keeps_mcp_and_low_frequency_office_only():
    deferred = collect_deferred(_tools())

    assert set(deferred) == {
        "PptxUpdateSlide",
        "mcp__github__list_pull_requests",
        "mcp__github__search_code",
        "mcp__12306-train__query_train_tickets",
    }
    assert deferred["PptxUpdateSlide"].group == "office"
    assert deferred["mcp__github__search_code"].group == "github"
    assert deferred["mcp__12306-train__query_train_tickets"].group == "12306-train"


def test_search_matches_tool_names_and_descriptions():
    deferred = collect_deferred(_tools())

    assert [item.name for item in search_deferred(deferred, "update slide")] == ["PptxUpdateSlide"]
    github = [item.name for item in search_deferred(deferred, "github pull request")]
    assert github[0] == "mcp__github__list_pull_requests"
    assert "github" in github[0]


def test_search_empty_query_returns_nothing():
    deferred = collect_deferred(_tools())

    assert search_deferred(deferred, "") == []
    assert search_deferred(deferred, "   ") == []


def test_catalog_skips_loaded_tools_and_names_the_rest():
    deferred = collect_deferred(_tools())

    block = render_deferred_catalog(
        deferred,
        active_names={"PptxUpdateSlide"},
    )

    assert block is not None
    assert block.startswith("<deferred-tools>")
    assert block.endswith("</deferred-tools>")
    assert "PptxUpdateSlide" not in block
    assert "mcp__github__list_pull_requests" in block
    assert "[github]" in block
    assert "[office]" not in block


def test_catalog_returns_none_when_everything_is_loaded():
    deferred = collect_deferred(_tools())

    assert render_deferred_catalog(deferred, active_names=set(deferred)) is None


def test_tool_search_activates_matches_and_returns_schemas():
    deferred = collect_deferred(_tools())
    registry = FakeRegistry()

    result = tool_search_result(deferred, registry, "github pull request")

    assert result["activated"][0] == "mcp__github__list_pull_requests"
    assert set(result["activated"]) <= {
        "mcp__github__list_pull_requests",
        "mcp__github__search_code",
    }
    assert registry.active_names == frozenset(result["activated"])
    assert result["tools"][0]["name"] == "mcp__github__list_pull_requests"
    assert result["tools"][0]["parameters"]["properties"]["query"]["type"] == "string"


def test_tool_search_empty_query_lists_without_activating():
    deferred = collect_deferred(_tools())
    registry = FakeRegistry({"Read"})

    result = tool_search_result(deferred, registry, "")

    assert result["activated"] == []
    assert {item["name"] for item in result["catalog"]} == set(deferred)
    assert registry.active_names == frozenset({"Read"})


def test_tool_search_unknown_query_returns_a_hint():
    deferred = collect_deferred(_tools())
    registry = FakeRegistry()

    result = tool_search_result(deferred, registry, "quantum flux capacitor")

    assert result["activated"] == []
    assert result["tools"] == []
    assert "ToolSearch" in result["hint"]
