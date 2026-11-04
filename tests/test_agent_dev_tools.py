"""Tests for the Claude Code-style developer tools in `server/app/agent/dev_tools.py`."""

import json
import os
import shutil
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from app.agent.dev_tools import DevToolConfig, build_dev_tools, load_dev_tool_config  # noqa: E402


class FakeTool:
    def __init__(self, fn, name):
        self.fn = fn
        self.name = name


class FakeSDK:
    def tool(self, fn, *, name=None):
        return FakeTool(fn, name or fn.__name__)


def _config(workspace: Path, **overrides) -> DevToolConfig:
    values = {
        "workspace": workspace.resolve(),
        "allow_shell": False,
        "command_timeout": 5.0,
        "max_output_bytes": 100_000,
        "max_file_bytes": 100_000,
        "web_timeout": 5.0,
        "web_max_bytes": 100_000,
        "web_allow_private": False,
    }
    values.update(overrides)
    return DevToolConfig(**values)


def _tools(workspace: Path, **overrides):
    return {tool.name: tool.fn for tool in build_dev_tools(FakeSDK(), _config(workspace, **overrides))}


# ---- configuration and registration -------------------------------------------


def test_disabled_tools_are_not_registered(monkeypatch):
    monkeypatch.setenv("AGENT_DEV_TOOLS", "false")

    assert load_dev_tool_config() is None
    assert build_dev_tools(FakeSDK()) == []


def test_config_defaults_and_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENT_TOOLS_ALLOW_SHELL", raising=False)

    config = load_dev_tool_config()

    assert config is not None
    assert config.workspace == tmp_path.resolve()
    assert config.allow_shell is False
    assert config.command_timeout == 120.0

    monkeypatch.setenv("AGENT_TOOLS_ALLOW_SHELL", "true")
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "9")
    config = load_dev_tool_config()

    assert config is not None
    assert config.allow_shell is True
    assert config.command_timeout == 9.0


def test_core_tools_registered_without_shell(tmp_path):
    names = set(_tools(tmp_path))

    assert names == {"Read", "Write", "Edit", "NotebookEdit", "Glob", "Grep", "WebFetch"}


def test_shell_tools_registered_only_when_allowed(tmp_path):
    names = set(_tools(tmp_path, allow_shell=True))

    assert {"Bash", "PowerShell", "Monitor"} <= names


# ---- Read / Write / Edit -------------------------------------------------------


def test_read_returns_numbered_lines_with_offset(tmp_path):
    (tmp_path / "notes.txt").write_text("one\ntwo\nthree\nfour\n")
    tools = _tools(tmp_path)

    result = tools["Read"]("notes.txt", offset=1, limit=2)

    assert result["total_lines"] == 4
    assert result["returned_lines"] == 2
    assert result["content"] == "2\ttwo\n3\tthree"
    assert result["truncated"] is True


def test_read_rejects_paths_outside_the_workspace(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret")
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="outside the workspace"):
        tools["Read"](str(outside))
    with pytest.raises(ValueError, match="outside the workspace"):
        tools["Read"]("../" + outside.name)


def test_read_rejects_symlink_escapes(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-linked.txt"
    outside.write_text("secret")
    (tmp_path / "link.txt").symlink_to(outside)
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="outside the workspace"):
        tools["Read"]("link.txt")


def test_read_renders_notebooks_and_images(tmp_path):
    notebook = {
        "cells": [
            {"cell_type": "markdown", "id": "a", "source": ["# Title"]},
            {"cell_type": "code", "id": "b", "source": ["print(1)"]},
        ]
    }
    (tmp_path / "demo.ipynb").write_text(json.dumps(notebook))
    (tmp_path / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    tools = _tools(tmp_path)

    rendered = tools["Read"]("demo.ipynb")
    image = tools["Read"]("pixel.png")

    assert rendered["type"] == "notebook"
    assert rendered["cell_count"] == 2
    assert "--- cell 0 [markdown] id=a ---" in rendered["content"]
    assert image["type"] == "image"
    assert image["content"] is None


def test_read_extracts_pdf_pages(tmp_path):
    pypdf = pytest.importorskip("pypdf", reason="pypdf not installed")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=72, height=72)
    with (tmp_path / "doc.pdf").open("wb") as handle:
        writer.write(handle)
    tools = _tools(tmp_path)

    result = tools["Read"]("doc.pdf", pages="2")

    assert result["type"] == "pdf"
    assert result["page_count"] == 2
    assert result["pages"] == [2]
    assert "--- page 2 ---" in result["content"]


def test_read_rejects_binary_text_files(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"abc\x00def")
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="binary"):
        tools["Read"]("blob.bin")


def test_write_creates_parents_and_reports_overwrite(tmp_path):
    tools = _tools(tmp_path)

    first = tools["Write"]("nested/dir/file.txt", "hello")
    second = tools["Write"]("nested/dir/file.txt", "hello again")

    assert first == {"path": "nested/dir/file.txt", "bytes": 5, "created": True}
    assert second["created"] is False
    assert (tmp_path / "nested/dir/file.txt").read_text() == "hello again"


def test_write_enforces_the_file_size_limit(tmp_path):
    tools = _tools(tmp_path, max_file_bytes=10)

    with pytest.raises(ValueError, match="too large"):
        tools["Write"]("big.txt", "x" * 11)


def test_edit_requires_unique_matches_unless_replace_all(tmp_path):
    (tmp_path / "code.py").write_text("value = 1\nother = 1\n")
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="occurs 2 times"):
        tools["Edit"]("code.py", "1", "2")

    result = tools["Edit"]("code.py", "1", "2", replace_all=True)

    assert result["replacements"] == 2
    assert (tmp_path / "code.py").read_text() == "value = 2\nother = 2\n"


def test_edit_reports_missing_text(tmp_path):
    (tmp_path / "code.py").write_text("value = 1\n")
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        tools["Edit"]("code.py", "absent", "x")


# ---- NotebookEdit ---------------------------------------------------------------


def _notebook(path: Path) -> dict:
    document = {
        "cells": [
            {"cell_type": "markdown", "id": "a", "metadata": {}, "source": ["# A"]},
            {"cell_type": "code", "id": "b", "metadata": {}, "source": ["print(1)"], "outputs": []},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    path.write_text(json.dumps(document))
    return document


def test_notebook_edit_replace_insert_delete(tmp_path):
    path = tmp_path / "demo.ipynb"
    _notebook(path)
    tools = _tools(tmp_path)

    replaced = tools["NotebookEdit"]("demo.ipynb", "a", "# B", "markdown", "replace")
    inserted = tools["NotebookEdit"]("demo.ipynb", "a", "print(2)", "code", "insert")
    deleted = tools["NotebookEdit"]("demo.ipynb", "b", "", "code", "delete")

    document = json.loads(path.read_text())
    assert replaced["cell_index"] == 0
    assert inserted["cell_index"] == 1
    assert deleted["cell_count"] == 2
    assert document["cells"][0]["source"] == ["# B"]
    assert document["cells"][1]["source"] == ["print(2)"]
    assert all(cell.get("id") != "b" for cell in document["cells"])


def test_notebook_edit_rejects_unknown_cell(tmp_path):
    (tmp_path / "demo.ipynb").write_text(json.dumps({"cells": []}))
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="not found"):
        tools["NotebookEdit"]("demo.ipynb", "missing", "x")


# ---- Glob / Grep ----------------------------------------------------------------


def test_glob_finds_files_and_honours_limit(tmp_path):
    (tmp_path / "a.py").write_text("a")
    (tmp_path / "b.md").write_text("b")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("c")
    tools = _tools(tmp_path)

    top_level = tools["Glob"]("*.py")
    recursive = tools["Glob"]("**/*.py")
    limited = tools["Glob"]("**/*", limit=1)

    assert top_level["matches"] == ["a.py"]
    assert set(recursive["matches"]) == {"a.py", "sub/c.py"}
    assert limited["truncated"] is True


def test_glob_rejects_absolute_patterns(tmp_path):
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="relative"):
        tools["Glob"]("/etc/*")


def test_grep_supports_content_files_and_count(tmp_path):
    (tmp_path / "one.py").write_text("Alpha\nbeta\nALPHA again\n")
    (tmp_path / "two.md").write_text("nothing here\n")
    tools = _tools(tmp_path)

    content = tools["Grep"]("alpha", output_mode="content", case_insensitive=True)
    files = tools["Grep"]("alpha", case_insensitive=True)
    counts = tools["Grep"]("alpha", output_mode="count", case_insensitive=True)

    assert [match["line"] for match in content["matches"]] == [1, 3]
    assert files["matches"] == ["one.py"]
    assert counts["counts"] == [{"path": "one.py", "count": 2}]
    assert counts["total_matches"] == 2


def test_grep_filters_by_glob_and_rejects_bad_regex(tmp_path):
    (tmp_path / "one.py").write_text("alpha\n")
    (tmp_path / "two.md").write_text("alpha\n")
    tools = _tools(tmp_path)

    assert tools["Grep"]("alpha", glob="*.py")["matches"] == ["one.py"]
    with pytest.raises(ValueError, match="invalid regular expression"):
        tools["Grep"]("(")


# ---- Bash / PowerShell / Monitor -------------------------------------------------


def test_bash_returns_output_and_exit_code(tmp_path):
    tools = _tools(tmp_path, allow_shell=True)

    result = tools["Bash"]("printf 'hello'")

    assert result["exit_code"] == 0
    assert result["stdout"] == "hello"
    assert result["timed_out"] is False


def test_bash_reports_nonzero_exit_and_timeout(tmp_path):
    tools = _tools(tmp_path, allow_shell=True)

    failed = tools["Bash"]("echo oops >&2; exit 3")
    timed_out = tools["Bash"]("sleep 2", timeout_seconds=1)

    assert failed["exit_code"] == 3
    assert failed["stderr"].strip() == "oops"
    assert timed_out["timed_out"] is True
    assert timed_out["exit_code"] is None


def test_powershell_reports_a_missing_runtime(tmp_path):
    tools = _tools(tmp_path, allow_shell=True)
    if shutil.which("pwsh") or shutil.which("powershell"):
        pytest.skip("pwsh is installed; the missing-runtime path does not apply")

    with pytest.raises(RuntimeError, match="not installed"):
        tools["PowerShell"]("Write-Output hi")


def test_monitor_streams_output_and_stops(tmp_path):
    tools = _tools(tmp_path, allow_shell=True)

    started = tools["Monitor"](
        "start",
        command="printf 'one\\n'; sleep 0.3; printf 'two\\n'",
        wait_seconds=0.1,
    )
    polled = tools["Monitor"]("poll", monitor_id=started["monitor_id"], wait_seconds=2)
    stopped = tools["Monitor"]("stop", monitor_id=started["monitor_id"])

    assert started["output"] == "one"
    assert "two" in polled["output"]
    assert polled["running"] is False
    assert stopped["action"] == "stop"
    assert stopped["running"] is False


def test_monitor_rejects_unknown_ids(tmp_path):
    tools = _tools(tmp_path, allow_shell=True)

    with pytest.raises(ValueError, match="unknown monitor_id"):
        tools["Monitor"]("poll", monitor_id="nope")


# ---- WebFetch --------------------------------------------------------------------


class _PageHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b"<html><head><title>T</title><style>x{}</style></head><body><h1>Hello</h1><p>World</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _CountingPageHandler(_PageHandler):
    hits = 0

    def do_GET(self):
        type(self).hits += 1
        super().do_GET()


class _HtmlWithoutContentTypeHandler(_PageHandler):
    def do_GET(self):
        body = b"<!doctype html><html><head><title>T</title></head><body><h1>Hello</h1><p>World</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _PlainTextWithoutContentTypeHandler(_PageHandler):
    def do_GET(self):
        body = "价格约为 10 元\n第二行".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _MislabeledHtmlHandler(_PageHandler):
    def do_GET(self):
        body = b"<html><body><h1>Hello</h1><p>World</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _NotFoundPageHandler(_CountingPageHandler):
    def do_GET(self):
        type(self).hits += 1
        body = (
            "<!doctype html><html><head><title>404错误页-帮助中心</title></head>"
            "<body><nav>首页 | 文档</nav><p>页面不存在</p></body></html>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _ClientRenderedShellHandler(_PageHandler):
    def do_GET(self):
        body = (
            b"<!doctype html><html><head><title>App</title></head><body>"
            b'<div id="root"></div><script>window.__APP__ = 1;</script></body></html>'
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _PageDiscussing404Handler(_PageHandler):
    def do_GET(self):
        paragraph = "HTTP 状态码文档：服务端返回 404 表示资源不存在，客户端应提示用户检查链接。" * 40
        body = f"<html><head><title>HTTP 404 状态码</title></head><body><p>{paragraph}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def _serve(handler_cls):
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


@pytest.fixture
def page_server():
    with _serve(_PageHandler) as url:
        yield url


def test_webfetch_reuses_a_url_within_one_run(tmp_path):
    _CountingPageHandler.hits = 0
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_CountingPageHandler) as url:
        first = tools["WebFetch"](url)
        second = tools["WebFetch"](url + "#pricing")

    assert _CountingPageHandler.hits == 1
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["content"] == first["content"]


def test_webfetch_cache_is_scoped_to_one_run(tmp_path):
    _CountingPageHandler.hits = 0

    with _serve(_CountingPageHandler) as url:
        _tools(tmp_path, web_allow_private=True)["WebFetch"](url)
        _tools(tmp_path, web_allow_private=True)["WebFetch"](url)

    assert _CountingPageHandler.hits == 2


def test_webfetch_cache_does_not_store_failures(tmp_path):
    _NotFoundPageHandler.hits = 0
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_NotFoundPageHandler) as url:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="reports itself as not found"):
                tools["WebFetch"](url)

    assert _NotFoundPageHandler.hits == 2


def test_webfetch_blocks_private_addresses_by_default(tmp_path, page_server):
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="non-public address"):
        tools["WebFetch"](page_server)


def test_webfetch_extracts_html_text(tmp_path, page_server):
    tools = _tools(tmp_path, web_allow_private=True)

    result = tools["WebFetch"](page_server)

    assert result["status"] == 200
    assert "Hello" in result["content"]
    assert "World" in result["content"]
    assert "<h1>" not in result["content"]
    assert "x{}" not in result["content"]


def test_webfetch_extracts_html_when_content_type_is_missing(tmp_path):
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_HtmlWithoutContentTypeHandler) as url:
        result = tools["WebFetch"](url)

    assert result["content_type"] == "unknown"
    assert "Hello" in result["content"]
    assert "World" in result["content"]
    assert "<h1>" not in result["content"]
    assert "<!doctype" not in result["content"].lower()


def test_webfetch_keeps_plain_text_when_content_type_is_missing(tmp_path):
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_PlainTextWithoutContentTypeHandler) as url:
        result = tools["WebFetch"](url)

    assert result["content"] == "价格约为 10 元\n第二行"


def test_webfetch_extracts_mislabeled_html(tmp_path):
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_MislabeledHtmlHandler) as url:
        result = tools["WebFetch"](url)

    assert result["content_type"] == "text/plain"
    assert "Hello" in result["content"]
    assert "World" in result["content"]
    assert "<h1>" not in result["content"]


def test_webfetch_reports_a_not_found_page_served_with_200(tmp_path):
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_NotFoundPageHandler) as url:
        with pytest.raises(RuntimeError, match="reports itself as not found"):
            tools["WebFetch"](url)


def test_webfetch_reports_a_client_rendered_shell(tmp_path):
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_ClientRenderedShellHandler) as url:
        with pytest.raises(RuntimeError, match="no readable text"):
            tools["WebFetch"](url)


def test_webfetch_keeps_long_pages_that_mention_404(tmp_path):
    tools = _tools(tmp_path, web_allow_private=True)

    with _serve(_PageDiscussing404Handler) as url:
        result = tools["WebFetch"](url)

    assert result["status"] == 200
    assert "资源不存在" in result["content"]


def test_webfetch_rejects_unsupported_schemes(tmp_path):
    tools = _tools(tmp_path)

    with pytest.raises(ValueError, match="http and https"):
        tools["WebFetch"]("file:///etc/passwd")
