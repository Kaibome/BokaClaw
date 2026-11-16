from __future__ import annotations

import errno
import json
import socket
import subprocess
import urllib.error

import pytest

from bokaclaw_agent import TransientToolError
from bokaclaw_agent.core.exceptions import ToolArgumentError, ToolExecutionError
from bokaclaw_agent.core.types import ToolCall
from bokaclaw_agent.tools import Tool, ToolRegistry
from bokaclaw_agent.tools.errors import ErrorClass, classify_tool_error


def test_transient_tool_error_is_transient_and_keeps_retry_after():
    classified = classify_tool_error(TransientToolError("upstream timed out", retry_after=2.5))

    assert classified == ErrorClass("transient", 2.5, "TransientToolError")


def test_transient_tool_error_ignores_invalid_retry_after():
    assert classify_tool_error(TransientToolError("boom", retry_after=-1)).retry_after is None
    assert classify_tool_error(TransientToolError("boom", retry_after=True)).retry_after is None


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("slow"),
        socket.timeout("slow"),
        ConnectionResetError("reset by peer"),
        ConnectionRefusedError("refused"),
        BrokenPipeError("broken pipe"),
        subprocess.TimeoutExpired(cmd="tool", timeout=1),
        OSError(errno.ETIMEDOUT, "timed out"),
        OSError(errno.ECONNRESET, "reset"),
    ],
)
def test_builtin_transient_errors(error):
    assert classify_tool_error(error).kind == "transient"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("bad input"),
        TypeError("bad type"),
        KeyError("missing"),
        AttributeError("missing attribute"),
        PermissionError("denied"),
        FileNotFoundError("missing file"),
        IsADirectoryError("is a directory"),
        ToolArgumentError("invalid arguments"),
    ],
)
def test_permanent_errors(error):
    assert classify_tool_error(error).kind == "permanent"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("unexpected"),
        OSError(errno.EIO, "io error"),
        ZeroDivisionError("division by zero"),
    ],
)
def test_unknown_errors_are_not_retryable(error):
    assert classify_tool_error(error).kind == "unknown"


def test_url_errors_are_classified_by_status():
    not_found = urllib.error.HTTPError("https://example.com", 404, "Not Found", {}, None)
    rate_limited = urllib.error.HTTPError("https://example.com", 429, "Too Many Requests", {}, None)
    unavailable = urllib.error.HTTPError("https://example.com", 503, "Unavailable", {}, None)

    assert classify_tool_error(not_found).kind == "permanent"
    assert classify_tool_error(rate_limited).kind == "transient"
    assert classify_tool_error(unavailable).kind == "transient"
    assert classify_tool_error(urllib.error.URLError("dns failure")).kind == "transient"


def test_already_classified_execution_error_is_preserved():
    error = ToolExecutionError(
        "wrapped",
        kind="transient",
        cause_type="TimeoutError",
        retry_after=1.5,
    )

    assert classify_tool_error(error) == ErrorClass("transient", 1.5, "TimeoutError")


def test_unclassified_execution_error_inspects_its_cause():
    try:
        try:
            raise TimeoutError("slow")
        except TimeoutError as cause:
            raise ToolExecutionError("wrapped") from cause
    except ToolExecutionError as error:
        assert classify_tool_error(error) == ErrorClass("transient", None, "TimeoutError")


def test_httpx_network_errors_are_transient():
    httpx = pytest.importorskip("httpx")

    assert classify_tool_error(httpx.ConnectError("refused")).kind == "transient"
    assert classify_tool_error(httpx.ReadTimeout("timeout")).kind == "transient"


def test_tool_invoke_attaches_classification():
    def flaky() -> str:
        raise TransientToolError("upstream timed out", retry_after=3)

    tool = Tool.from_callable(flaky, name="Flaky", effect="external")

    with pytest.raises(ToolExecutionError) as info:
        tool.invoke({}, tool_call_id="call-1")

    assert info.value.kind == "transient"
    assert info.value.cause_type == "TransientToolError"
    assert info.value.retry_after == 3.0
    assert "upstream timed out" in str(info.value)


def test_tool_invoke_classifies_permanent_errors():
    def invalid() -> str:
        raise ValueError("bad input")

    tool = Tool.from_callable(invalid, name="Invalid", effect="read")

    with pytest.raises(ToolExecutionError) as info:
        tool.invoke({}, tool_call_id="call-2")

    assert info.value.kind == "permanent"
    assert info.value.cause_type == "ValueError"
    assert info.value.retry_after is None


def test_unknown_error_message_and_result_are_unchanged():
    def boom() -> str:
        raise RuntimeError("boom")

    registry = ToolRegistry([Tool.from_callable(boom, name="Boom", effect="external")])
    result = registry.invoke(ToolCall(id="call-3", name="Boom", arguments={}))

    assert result.is_error
    assert result.metadata == {}
    assert json.loads(result.content) == {"error": "Tool 'Boom' failed: boom"}
