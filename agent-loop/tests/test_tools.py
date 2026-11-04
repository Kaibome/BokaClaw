from __future__ import annotations

import json
from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel

from bokaclaw_agent.core.exceptions import (
    ToolArgumentError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolSchemaError,
)
from bokaclaw_agent.core.types import ToolCall
from bokaclaw_agent.tools import Tool, ToolRegistry, tool


class Color(str, Enum):
    RED = "red"
    BLUE = "blue"


class Address(BaseModel):
    city: str
    zip_code: str


def test_schema_from_type_hints_and_docstring():
    @tool
    def search(query: str, limit: int = 5, tags: list[str] | None = None) -> str:
        """Search the knowledge base.

        Args:
            query: The search query.
            limit: Maximum number of results.
            tags: Optional tags to filter by.
        """
        return "ok"

    assert search.name == "search"
    assert search.description.startswith("Search the knowledge base.")
    assert search.parameters["type"] == "object"
    assert search.parameters["properties"]["query"]["type"] == "string"
    assert search.parameters["properties"]["query"]["description"] == "The search query."
    assert search.parameters["properties"]["limit"]["description"] == "Maximum number of results."
    assert search.parameters["required"] == ["query"]


def test_schema_supports_literal_enum_and_nested_model():
    @tool
    def create(address: Address, color: Color, mode: Literal["fast", "slow"] = "fast") -> str:
        """Create a record."""
        return "ok"

    properties = create.parameters["properties"]
    assert "address" in properties
    assert "color" in properties
    assert "mode" in properties
    assert create.parameters["required"] == ["address", "color"]
    assert "$defs" in create.parameters


def test_tool_name_and_description_override():
    @tool(name="custom_name", description="Custom description")
    def original(value: int) -> int:
        return value

    assert original.name == "custom_name"
    assert original.description == "Custom description"


def test_explicit_tool_accepts_manual_schema():
    def echo(value: str) -> str:
        return value

    manual = Tool(
        name="echo",
        description="Echo a value.",
        parameters={"type": "object", "properties": {"value": {"type": "string"}}},
        func=echo,
    )
    result = manual.invoke({"value": "hello"})
    assert result.name == "echo"
    assert result.content == "hello"


def test_invoke_coerces_and_uses_defaults():
    @tool
    def weather(city: str, unit: str = "celsius") -> str:
        """Get weather."""
        return f"{city}:{unit}"

    result = weather.invoke({"city": "Beijing"})
    assert result.content == "Beijing:celsius"


def test_invoke_rejects_invalid_arguments():
    @tool
    def add(a: int, b: int) -> int:
        """Add two ints."""
        return a + b

    with pytest.raises(ToolArgumentError):
        add.invoke({"a": 1})


def test_invoke_wraps_tool_exceptions():
    @tool
    def boom() -> str:
        """Always fails."""
        raise ValueError("nope")

    with pytest.raises(ToolExecutionError, match="nope"):
        boom.invoke({})


def test_async_tool_is_detected_and_executed():
    @tool
    async def async_add(a: int, b: int) -> int:
        """Add asynchronously."""
        return a + b

    assert async_add.is_async is True
    assert async_add.invoke({"a": 2, "b": 3}).content == "5"


def test_return_value_serialization():
    @tool
    def returns_dict() -> dict:
        """Return a dict."""
        return {"a": 1}

    @tool
    def returns_none() -> None:
        """Return nothing."""
        return None

    class Output(BaseModel):
        value: int

    @tool
    def returns_model() -> Output:
        """Return a model."""
        return Output(value=7)

    assert json.loads(returns_dict.invoke({}).content) == {"a": 1}
    assert returns_none.invoke({}).content == "null"
    assert json.loads(returns_model.invoke({}).content) == {"value": 7}


def test_registry_specs_and_dispatch():
    @tool
    def add(a: int, b: int) -> int:
        """Add two ints."""
        return a + b

    registry = ToolRegistry([add])
    spec = registry.specs()[0]
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "add"

    result = registry.invoke(ToolCall(id="call_1", name="add", arguments={"a": 1, "b": 2}))
    assert result.tool_call_id == "call_1"
    assert result.content == "3"
    assert result.is_error is False


def test_registry_unknown_tool_returns_error_result():
    registry = ToolRegistry()
    result = registry.invoke(ToolCall(id="call_1", name="missing", arguments={}))
    assert result.is_error is True
    assert "Unknown tool" in json.loads(result.content)["error"]


def test_registry_unknown_tool_raises_in_raise_mode():
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.invoke(ToolCall(id="call_1", name="missing", arguments={}), error_mode="raise")


def test_registry_reports_invalid_json_arguments():
    @tool
    def add(a: int, b: int) -> int:
        """Add two ints."""
        return a + b

    registry = ToolRegistry([add])
    call = ToolCall(id="call_1", name="add", arguments={}, arguments_error="Expecting value")
    result = registry.invoke(call)
    assert result.is_error is True
    assert "Invalid JSON arguments" in result.content


def test_registry_rejects_duplicate_names():
    @tool
    def add(a: int, b: int) -> int:
        """Add."""
        return a + b

    registry = ToolRegistry([add])
    with pytest.raises(ToolSchemaError):
        registry.register(add)


def test_variadic_parameters_are_rejected():
    with pytest.raises(ToolSchemaError):

        @tool
        def bad(*args: int) -> int:
            """Bad tool."""
            return sum(args)
