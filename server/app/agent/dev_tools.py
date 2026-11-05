"""Claude Code-style developer tools for the in-process agent runtime.

Filesystem tools are confined to ``AGENT_WORKSPACE_ROOT``; any path that
resolves outside it is rejected, including through symlinks. Shell tools
(``Bash``, ``PowerShell``, ``Monitor``) are registered only when
``AGENT_TOOLS_ALLOW_SHELL=true``. When ``AGENT_EXECUTOR_URL`` is set they are
delegated to the sandboxed executor container (see ``agent_executor.py``);
otherwise they run in the server process, where there is no OS-level sandbox.

The module deliberately has no import-time side effects: tools are built per
run, and the workspace directory is created on first use.
"""

import atexit
import fnmatch
import ipaddress
import json
import logging
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urljoin, urlparse, urlunparse
from xml.etree import ElementTree

import httpx

from app.agent import executor as agent_executor

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE = "/app/workspace"
DEFAULT_MAX_OUTPUT_BYTES = 200_000
DEFAULT_MAX_FILE_BYTES = 2_000_000
DEFAULT_WEB_MAX_BYTES = 1_000_000
DEFAULT_SEARCH_RESULTS = 5
MAX_SEARCH_RESULTS = 10
# Bing's RSS view is a stable, script-free search API and works where the HTML
# search pages are blocked or rate limited.
BING_RSS_ENDPOINT = "https://www.bing.com/search"
MAX_READ_LINES = 5000
MAX_MONITOR_LINES = 5000
MAX_REDIRECTS = 5
# A repeated WebFetch returns this much of the cached page unless full=True.
CACHED_DIGEST_CHARS = 1_200

# Search engines reject non-browser clients; WebFetch keeps its own agent UA.
SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_SEARCH_HEADERS = {
    "User-Agent": SEARCH_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
}

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}
SKIP_DIRECTORIES = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".next"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DevToolConfig:
    """Settings for the developer tool set."""

    workspace: Path
    allow_shell: bool
    command_timeout: float
    max_output_bytes: int
    max_file_bytes: int
    web_timeout: float
    web_max_bytes: int
    web_allow_private: bool
    web_search_url: str = ""
    executor: Optional[agent_executor.ExecutorConfig] = None
    office_tools: bool = True


def load_dev_tool_config() -> Optional[DevToolConfig]:
    """Return the configured tool set, or ``None`` when ``AGENT_DEV_TOOLS`` is off."""
    if not _env_bool("AGENT_DEV_TOOLS", True):
        return None

    workspace = Path(os.environ.get("AGENT_WORKSPACE_ROOT") or DEFAULT_WORKSPACE).expanduser()
    return DevToolConfig(
        workspace=workspace.resolve(),
        allow_shell=_env_bool("AGENT_TOOLS_ALLOW_SHELL", False),
        command_timeout=max(1.0, _env_float("AGENT_TOOL_TIMEOUT_SECONDS", 120.0)),
        max_output_bytes=max(1000, _env_int("AGENT_TOOL_MAX_OUTPUT_BYTES", DEFAULT_MAX_OUTPUT_BYTES)),
        max_file_bytes=max(1000, _env_int("AGENT_TOOL_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES)),
        web_timeout=max(1.0, _env_float("AGENT_WEB_FETCH_TIMEOUT_SECONDS", 30.0)),
        web_max_bytes=max(1000, _env_int("AGENT_WEB_FETCH_MAX_BYTES", DEFAULT_WEB_MAX_BYTES)),
        web_allow_private=_env_bool("AGENT_WEB_FETCH_ALLOW_PRIVATE", False),
        web_search_url=(os.environ.get("AGENT_WEB_SEARCH_URL") or "").strip(),
        executor=agent_executor.load_executor_config(),
        office_tools=_env_bool("AGENT_OFFICE_TOOLS", True),
    )


def _workspace_root(config: DevToolConfig) -> Path:
    try:
        config.workspace.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"Workspace {config.workspace} is not usable: {exc}") from exc
    return config.workspace


def _relative(config: DevToolConfig, path: Path) -> str:
    try:
        return path.relative_to(config.workspace).as_posix()
    except ValueError:  # pragma: no cover - guarded by _resolve
        return str(path)


def _resolve(config: DevToolConfig, value: str, *, must_exist: bool) -> Path:
    """Resolve ``value`` inside the workspace, rejecting escapes via ``..`` or symlinks."""
    if not value or not value.strip():
        raise ValueError("path must not be empty")

    root = _workspace_root(config)
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"path {value!r} is outside the workspace {root}") from None

    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"path {value!r} does not exist")
    return resolved


def _truncate(text: str, max_bytes: int) -> tuple[str, bool]:
    """Keep the head and tail of an oversized string, marking the omission."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text, False

    marker = "\n... [truncated] ...\n"
    budget = max(0, max_bytes - len(marker.encode()))
    head = encoded[: budget // 2].decode("utf-8", errors="ignore")
    tail = encoded[-(budget - budget // 2) :].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}", True


def _read_text_file(path: Path, max_bytes: int) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"file is too large to edit ({size} bytes > {max_bytes} bytes)")
    data = path.read_bytes()
    if b"\x00" in data[:8192]:
        raise ValueError("file appears to be binary; use Read to inspect it")
    return data.decode("utf-8", errors="replace")


def _parse_page_range(spec: str, page_count: int) -> List[int]:
    if not spec.strip():
        return list(range(page_count))
    pages: List[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            start = int(start_text) if start_text else 1
            end = int(end_text) if end_text else page_count
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(part))
    selected = sorted({page - 1 for page in pages if 1 <= page <= page_count})
    if not selected:
        raise ValueError(f"page range {spec!r} does not select any page (document has {page_count})")
    return selected


def _render_notebook(document: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for index, cell in enumerate(document.get("cells") or []):
        source = cell.get("source") or ""
        if isinstance(source, list):
            source = "".join(source)
        cell_type = cell.get("cell_type", "unknown")
        cell_id = cell.get("id", "")
        header = f"--- cell {index} [{cell_type}]"
        if cell_id:
            header += f" id={cell_id}"
        chunks.append(f"{header} ---\n{source}")
    return "\n\n".join(chunks)


_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
# Servers that omit or undersell Content-Type still send HTML; sniff those bodies
# so the model never receives raw markup. Anything else keeps its exact bytes.
_HTML_SNIFF_CONTENT_TYPES = {"", "text/plain", "application/octet-stream", "binary/octet-stream"}
_HTML_PREFIXES = ("<!doctype", "<html", "<head", "<body")
# Leading whitespace and a byte-order mark are common in scraped pages.
_HTML_LEADING_TRIM = " \t\r\n" + chr(0xFEFF)


def _looks_like_html(text: str) -> bool:
    head = text[:8192].lstrip(_HTML_LEADING_TRIM).lower()
    return head.startswith(_HTML_PREFIXES)


# Sites answer unknown routes with an error page under HTTP 200, or with a bare
# client-rendered app shell. Both look successful to the model but carry no
# content, so they are reported as tool errors instead.
_TITLE_PATTERN = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_PATTERN = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>")
_NOT_FOUND_MARKERS = (
    "404",
    "not found",
    "does not exist",
    "no longer available",
    "页面不存在",
    "页面未找到",
    "找不到页面",
    "页面找不到",
    "错误页",
)
# Content-rich pages that merely discuss 404s are not error pages.
_NOT_FOUND_MAX_CHARS = 1500


def _heading_text(markup: str, pattern: re.Pattern[str]) -> str:
    match = pattern.search(markup)
    if not match:
        return ""
    text = _TAG_PATTERN.sub(" ", match.group(1))
    return re.sub(r"\s+", " ", text).strip().lower()


def _page_problem(markup: str, text: str) -> Optional[str]:
    """Return why an HTTP 200 HTML body is unusable, or ``None`` when it is fine."""
    title = _heading_text(markup, _TITLE_PATTERN)
    if not text or text.strip().lower() == title:
        # Only the <title> survived extraction: the body is an app shell.
        return "it rendered no readable text (client-side only page)"
    if len(text) > _NOT_FOUND_MAX_CHARS:
        return None
    heading = title or _heading_text(markup, _H1_PATTERN)
    if any(marker in heading for marker in _NOT_FOUND_MARKERS):
        return f"it reports itself as not found ({heading[:80]})"
    return None


def _html_to_text(markup: str) -> str:
    class _Extractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: List[str] = []
            self.skip_depth = 0

        def handle_starttag(self, tag: str, attrs) -> None:
            if tag in {"script", "style", "noscript"}:
                self.skip_depth += 1
            elif tag in {"td", "th"}:
                # Table cells would otherwise run together ("RegionPrice"),
                # which makes pricing and comparison tables unreadable.
                if self._last_char() != "\n":
                    self.parts.append(" | ")
            elif tag in {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
                self.parts.append("\n")

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style", "noscript"} and self.skip_depth:
                self.skip_depth -= 1
            elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
                self.parts.append("\n")

        def handle_data(self, data: str) -> None:
            if not self.skip_depth:
                self.parts.append(data)

        def _last_char(self) -> str:
            for part in reversed(self.parts):
                stripped = part.rstrip()
                if stripped:
                    return stripped[-1]
            return "\n"

    extractor = _Extractor()
    extractor.feed(markup)
    text = "".join(extractor.parts)
    lines: List[str] = []
    for raw in text.splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        line = re.sub(r"(?:\s*\|\s*)+", " | ", line)
        line = re.sub(r"^\| ?| ?\|$", "", line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _validate_public_url(url: str, allow_private: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only http and https URLs are supported")
    if not parsed.hostname:
        raise ValueError("URL is missing a hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed")
    if allow_private:
        return

    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or 80)}
    except socket.gaierror as exc:
        raise RuntimeError(f"could not resolve {parsed.hostname}: {exc}") from exc

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"{parsed.hostname} resolves to a non-public address ({address}); "
                "set AGENT_WEB_FETCH_ALLOW_PRIVATE=true to allow it"
            )


def _normalize_fetch_url(url: str) -> str:
    """Build the cache key for a URL.

    Fragments never reach the server, so they are dropped; scheme and host are
    case-insensitive. Path, parameters, and query are kept verbatim because
    they select different resources.
    """
    parsed = urlparse(url.strip())
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.params, parsed.query, "")
    )


def _fetch_url(
    config: DevToolConfig,
    url: str,
    *,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    full: bool = False,
) -> Dict[str, Any]:
    """Fetch a URL, reusing a result already fetched in this run.

    ``cache`` is scoped to one agent run by ``build_dev_tools``. Failed fetches
    raise before anything is stored, so a later attempt still hits the network.
    A cache hit returns a short digest by default so a repeated call cannot
    re-inject a large page into the context; ``full=True`` returns the whole
    cached content.
    """
    key = _normalize_fetch_url(url)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            note = "Already fetched earlier in this run; reuse this content instead of fetching it again."
            existing = cached.get("note")
            result = {
                **cached,
                "cached": True,
                "note": f"{existing} {note}" if existing else note,
            }
            content = str(result.get("content") or "")
            if not full and len(content) > CACHED_DIGEST_CHARS:
                result["content"] = (
                    content[:CACHED_DIGEST_CHARS]
                    + "\n... [shortened digest of the cached page; call WebFetch with full=true "
                    "for the rest] ..."
                )
                result["digest"] = True
                result["note"] += (
                    " This is a shortened digest of the cached page; call WebFetch again with "
                    "full=true if you need the whole content."
                )
            return result

    result = _fetch_url_uncached(config, url)
    if cache is not None:
        cache[key] = result
    return {**result, "cached": False}


def _fetch_url_uncached(config: DevToolConfig, url: str) -> Dict[str, Any]:
    current = url
    response: Optional[httpx.Response] = None
    body = b""

    with httpx.Client(timeout=config.web_timeout, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            _validate_public_url(current, config.web_allow_private)
            try:
                with client.stream("GET", current, headers={"User-Agent": "BokaClaw-Agent/1.0"}) as streamed:
                    if streamed.status_code in {301, 302, 303, 307, 308}:
                        location = streamed.headers.get("location")
                        if not location:
                            raise RuntimeError(f"{current} returned {streamed.status_code} without a Location header")
                        current = urljoin(current, location)
                        continue
                    if streamed.status_code >= 400:
                        raise RuntimeError(
                            f"request to {current} failed with HTTP {streamed.status_code}; the page does "
                            "not exist or is unavailable. Use WebSearch to find the current URL instead "
                            "of guessing."
                        )

                    chunks: List[bytes] = []
                    total = 0
                    for chunk in streamed.iter_bytes():
                        total += len(chunk)
                        if total > config.web_max_bytes:
                            break
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    response = streamed
                    break
            except httpx.HTTPError as exc:
                raise RuntimeError(f"could not fetch {current}: {exc}") from exc
        else:
            raise RuntimeError(f"too many redirects (more than {MAX_REDIRECTS})")

    if response is None:  # pragma: no cover - loop always breaks or raises
        raise RuntimeError("request produced no response")

    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    encoding = response.encoding or "utf-8"
    text = body.decode(encoding, errors="replace")
    if content_type in _HTML_CONTENT_TYPES or (content_type in _HTML_SNIFF_CONTENT_TYPES and _looks_like_html(text)):
        markup = text
        text = _html_to_text(markup)
        problem = _page_problem(markup, text)
        if problem:
            raise RuntimeError(
                f"request to {response.url} returned HTTP {response.status_code} but {problem}. "
                "Use WebSearch to find another source."
            )

    text, truncated = _truncate(text, config.max_output_bytes)
    final_url = str(response.url)
    result: Dict[str, Any] = {
        "url": final_url,
        "status": response.status_code,
        "content_type": content_type or "unknown",
        "content": text,
        "truncated": truncated or len(body) >= config.web_max_bytes,
    }
    if _normalize_fetch_url(final_url) != _normalize_fetch_url(url):
        # A redirect that changes the path usually means the requested detail
        # page no longer exists and the site fell back to an index page; say so
        # instead of letting the model treat it as the requested content.
        result["requested_url"] = url
        result["note"] = (
            f"The requested URL redirected to {final_url}. If that is a listing or home page, "
            "the detail page may no longer exist; use WebSearch to find it."
        )
    return result


def _parse_bing_rss(text: str, limit: int) -> List[Dict[str, str]]:
    """Read Bing's RSS search response into title/url/snippet entries."""
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise RuntimeError("Bing returned a malformed RSS response") from exc

    results: List[Dict[str, str]] = []
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": re.sub(r"\s+", " ", item.findtext("title") or "").strip(),
                "url": url,
                "snippet": re.sub(r"\s+", " ", item.findtext("description") or "").strip(),
            }
        )
        if len(results) >= limit:
            break
    return results


def _search_results_from_payload(payload: Any, limit: int) -> List[Dict[str, str]]:
    """Read SearXNG's JSON ``results`` array."""
    if not isinstance(payload, dict):
        raise RuntimeError("search endpoint returned an unexpected payload")
    results: List[Dict[str, str]] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        results.append(
            {
                "title": re.sub(r"\s+", " ", str(item.get("title") or "")).strip(),
                "url": url,
                "snippet": re.sub(r"\s+", " ", str(item.get("content") or "")).strip(),
            }
        )
        if len(results) >= limit:
            break
    return results


def _request_search(
    config: DevToolConfig,
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, str]] = None,
) -> str:
    with httpx.Client(timeout=config.web_timeout, follow_redirects=True) as client:
        try:
            with client.stream(method, url, params=params, headers=_SEARCH_HEADERS) as streamed:
                if streamed.status_code >= 400:
                    raise RuntimeError(f"search request to {streamed.url} failed with HTTP {streamed.status_code}")
                chunks: List[bytes] = []
                total = 0
                for chunk in streamed.iter_bytes():
                    total += len(chunk)
                    if total > config.web_max_bytes:
                        break
                    chunks.append(chunk)
                body = b"".join(chunks)
                return body.decode(streamed.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            raise RuntimeError(f"could not reach the search endpoint {url}: {exc}") from exc


def _search_web(config: DevToolConfig, query: str, max_results: int) -> Dict[str, Any]:
    """Search with a configured SearXNG endpoint, or Bing RSS when unset."""
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(max_results), MAX_SEARCH_RESULTS))

    if config.web_search_url:
        base = config.web_search_url.rstrip("/")
        url = base if base.endswith("/search") else f"{base}/search"
        _validate_public_url(url, config.web_allow_private)
        body = _request_search(config, "GET", url, params={"q": query, "format": "json"})
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"search endpoint {url} did not return JSON; "
                "point AGENT_WEB_SEARCH_URL at a SearXNG instance or leave it empty"
            ) from exc
        results = _search_results_from_payload(payload, limit)
        provider = "searxng"
    else:
        _validate_public_url(BING_RSS_ENDPOINT, config.web_allow_private)
        body = _request_search(config, "GET", BING_RSS_ENDPOINT, params={"q": query, "format": "rss"})
        results = _parse_bing_rss(body, limit)
        provider = "bing"

    response: Dict[str, Any] = {
        "query": query,
        "provider": provider,
        "results": results,
        "truncated": len(results) >= limit,
    }
    if not results:
        response["note"] = "No results. Try different keywords, or fetch a known URL with WebFetch."
    return response


def _normalize_query(query: str) -> str:
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def _search_web_scoped(
    config: DevToolConfig,
    query: str,
    max_results: int,
    *,
    cache: Optional[Dict[str, Dict[str, Any]]] = None,
    seen_urls: Optional[Dict[str, str]] = None,
    fetched: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run a search and mark results this run has already seen.

    ``cache``, ``seen_urls`` and ``fetched`` are scoped to one agent run by
    ``build_dev_tools``. Repeating a query returns the earlier answer instead of
    searching again, and results already returned by another query or already
    fetched are annotated so the model can tell new evidence from repeats.
    """
    key = (_normalize_query(query), max(1, min(int(max_results), MAX_SEARCH_RESULTS)))
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            note = (
                "This exact search (same query and max_results) was already run in this run; "
                "reuse its results. Do not run it again."
            )
            existing = cached.get("note")
            return {**cached, "cached": True, "note": f"{existing} {note}" if existing else note}

    result = _search_web(config, query, max_results)
    results = result.get("results") or []
    if isinstance(results, list):
        repeats = 0
        for item in results:
            if not isinstance(item, dict) or not str(item.get("url") or "").strip():
                continue
            normalized = _normalize_fetch_url(str(item["url"]))
            if fetched and normalized in fetched:
                item["already_fetched"] = True
                repeats += 1
            elif seen_urls is not None and normalized in seen_urls:
                item["previously_seen"] = True
                repeats += 1
        if seen_urls is not None:
            for item in results:
                if isinstance(item, dict) and str(item.get("url") or "").strip():
                    seen_urls.setdefault(_normalize_fetch_url(str(item["url"])), result["query"])
        if results and repeats == len(results):
            result["note"] = (
                "Every result was already returned by an earlier search or fetch. Do not search again; "
                "fetch one of these URLs or answer from the facts you already have."
            )
    if cache is not None:
        cache[key] = result
    return result


class _Monitor:
    """A background process whose combined stdout/stderr is buffered line by line."""

    def __init__(self, process: subprocess.Popen, label: str) -> None:
        self.process = process
        self.label = label
        self.lines: List[str] = []
        self.running = True
        self.exit_code: Optional[int] = None
        self._condition = threading.Condition()
        self._thread = threading.Thread(target=self._read_output, daemon=True)
        self._thread.start()

    def _read_output(self) -> None:
        try:
            assert self.process.stdout is not None
            for line in self.process.stdout:
                with self._condition:
                    self.lines.append(line.rstrip("\r\n"))
                    if len(self.lines) > MAX_MONITOR_LINES:
                        del self.lines[: len(self.lines) - MAX_MONITOR_LINES]
                    self._condition.notify_all()
        finally:
            code = self.process.wait()
            with self._condition:
                self.running = False
                self.exit_code = code
                self._condition.notify_all()

    def read_new(self, wait_seconds: float, pattern: str) -> tuple[List[str], bool]:
        """Return lines accumulated since the last read, waiting up to ``wait_seconds``."""
        deadline = time.monotonic() + wait_seconds
        matcher = re.compile(pattern) if pattern else None
        with self._condition:
            while True:
                if matcher is not None and any(matcher.search(line) for line in self.lines):
                    break
                if not self.running:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)

            lines = list(self.lines)
            self.lines.clear()
            matched = bool(matcher and any(matcher.search(line) for line in lines))
            return lines, matched

    def terminate(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.process.kill()


_MONITORS: Dict[str, _Monitor] = {}
_MONITORS_LOCK = threading.Lock()


def _stop_all_monitors() -> None:
    with _MONITORS_LOCK:
        monitors = list(_MONITORS.values())
        _MONITORS.clear()
    for monitor in monitors:
        monitor.terminate()


atexit.register(_stop_all_monitors)


def _collect_grep_files(config: DevToolConfig, base: Path, pattern: str) -> List[Path]:
    if base.is_file():
        return [base]

    files: List[Path] = []
    for path in base.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRECTORIES for part in path.relative_to(base).parts):
            continue
        if pattern and not fnmatch.fnmatch(path.name, pattern) and not path.match(pattern):
            continue
        files.append(path)
    return sorted(files)


def execute_command(
    config: DevToolConfig,
    command: str,
    description: str,
    timeout_seconds: int,
    *,
    shell: str,
) -> Dict[str, Any]:
    """Run a command in the executor container when configured, else locally."""
    if config.executor is not None:
        return agent_executor.run_command(
            config.executor,
            command,
            description,
            timeout_seconds,
            shell=shell,
        )
    return run_command(config, command, description, timeout_seconds, shell=shell)


def execute_monitor(
    config: DevToolConfig,
    action: str,
    command: str = "",
    monitor_id: str = "",
    wait_seconds: float = 1.0,
    pattern: str = "",
) -> Dict[str, Any]:
    """Run a background command in the executor container when configured, else locally."""
    if config.executor is not None:
        return agent_executor.monitor_action(
            config.executor,
            action,
            command,
            monitor_id,
            wait_seconds,
            pattern,
        )
    return monitor_action(config, action, command, monitor_id, wait_seconds, pattern)


def build_dev_tools(sdk, config: Optional[DevToolConfig] = None):
    """Build the developer tool set, or return ``[]`` when disabled."""
    cfg = config if config is not None else load_dev_tool_config()
    if cfg is None:
        return []

    if cfg.allow_shell and cfg.executor is None:
        logger.warning("Shell tools are enabled without AGENT_EXECUTOR_URL; commands run inside the server process.")

    def Read(file_path: str, offset: int = 0, limit: int = 2000, pages: str = "") -> Dict[str, Any]:
        """Read a text file, notebook, or PDF from the workspace.

        Args:
            file_path: Path relative to the workspace (absolute paths inside it are allowed).
            offset: Zero-based line to start from for text files.
            limit: Maximum number of lines to return (1-5000).
            pages: PDF page selection such as "1-5" or "2,4"; empty means every page.
        """
        path = _resolve(cfg, file_path, must_exist=True)
        if not path.is_file():
            raise ValueError(f"{file_path!r} is not a file")

        suffix = path.suffix.lower()
        if suffix == ".ipynb":
            document = json.loads(_read_text_file(path, cfg.max_file_bytes))
            content, truncated = _truncate(_render_notebook(document), cfg.max_output_bytes)
            return {
                "path": _relative(cfg, path),
                "type": "notebook",
                "cell_count": len(document.get("cells") or []),
                "content": content,
                "truncated": truncated,
            }

        if suffix == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError as exc:
                raise RuntimeError("PDF support requires the pypdf package in the server image.") from exc
            reader = PdfReader(str(path))
            selected = _parse_page_range(pages, len(reader.pages))
            sections = [f"--- page {index + 1} ---\n{reader.pages[index].extract_text() or ''}" for index in selected]
            content, truncated = _truncate("\n\n".join(sections), cfg.max_output_bytes)
            return {
                "path": _relative(cfg, path),
                "type": "pdf",
                "page_count": len(reader.pages),
                "pages": [index + 1 for index in selected],
                "content": content,
                "truncated": truncated,
            }

        if suffix in IMAGE_SUFFIXES:
            return {
                "path": _relative(cfg, path),
                "type": "image",
                "mime_type": mimetypes.guess_type(str(path))[0] or "application/octet-stream",
                "size_bytes": path.stat().st_size,
                "content": None,
                "note": "This runtime sends text only, so image pixels are not visible to the model.",
            }

        if path.stat().st_size > cfg.max_file_bytes:
            raise ValueError(f"file is too large to read ({path.stat().st_size} bytes)")

        text = _read_text_file(path, cfg.max_file_bytes)
        lines = text.splitlines()
        start = max(0, offset)
        count = max(1, min(limit, MAX_READ_LINES))
        selected = lines[start : start + count]
        content = "\n".join(f"{start + index + 1}\t{line}" for index, line in enumerate(selected))
        content, truncated = _truncate(content, cfg.max_output_bytes)
        return {
            "path": _relative(cfg, path),
            "type": "text",
            "total_lines": len(lines),
            "offset": start,
            "returned_lines": len(selected),
            "content": content,
            "truncated": truncated or start + len(selected) < len(lines),
        }

    def Write(file_path: str, content: str) -> Dict[str, Any]:
        """Create a file or overwrite it entirely with new content.

        Args:
            file_path: Path relative to the workspace.
            content: Full file content to write.
        """
        path = _resolve(cfg, file_path, must_exist=False)
        if path.exists() and path.is_dir():
            raise ValueError(f"{file_path!r} is a directory")
        if len(content.encode("utf-8")) > cfg.max_file_bytes:
            raise ValueError(f"content is too large to write (limit {cfg.max_file_bytes} bytes)")
        path.parent.mkdir(parents=True, exist_ok=True)
        existed = path.exists()
        path.write_text(content, encoding="utf-8")
        return {
            "path": _relative(cfg, path),
            "bytes": len(content.encode("utf-8")),
            "created": not existed,
        }

    def Edit(file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> Dict[str, Any]:
        """Replace an exact string in a text file.

        Args:
            file_path: Path relative to the workspace.
            old_string: Exact text to replace. Must be unique unless replace_all is true.
            new_string: Replacement text.
            replace_all: Replace every occurrence instead of requiring a unique match.
        """
        if not old_string:
            raise ValueError("old_string must not be empty")
        path = _resolve(cfg, file_path, must_exist=True)
        if not path.is_file():
            raise ValueError(f"{file_path!r} is not a file")

        text = _read_text_file(path, cfg.max_file_bytes)
        occurrences = text.count(old_string)
        if occurrences == 0:
            raise ValueError("old_string was not found in the file")
        if occurrences > 1 and not replace_all:
            raise ValueError(f"old_string occurs {occurrences} times; add more context or pass replace_all=true")

        updated = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
        path.write_text(updated, encoding="utf-8")
        return {
            "path": _relative(cfg, path),
            "replacements": occurrences if replace_all else 1,
            "bytes": len(updated.encode("utf-8")),
        }

    def NotebookEdit(
        notebook_path: str,
        cell_id: str,
        new_source: str,
        cell_type: Literal["code", "markdown", "raw"] = "code",
        edit_mode: Literal["replace", "insert", "delete"] = "replace",
    ) -> Dict[str, Any]:
        """Edit one cell of a Jupyter notebook.

        Args:
            notebook_path: Path to the .ipynb file, relative to the workspace.
            cell_id: Cell id, or a numeric index when the notebook has no cell ids.
            new_source: New cell source. Ignored when edit_mode is delete.
            cell_type: Cell type for replace or insert.
            edit_mode: replace an existing cell, insert after it, or delete it.
        """
        path = _resolve(cfg, notebook_path, must_exist=True)
        if path.suffix.lower() != ".ipynb":
            raise ValueError("notebook_path must point to an .ipynb file")

        document = json.loads(_read_text_file(path, cfg.max_file_bytes))
        cells: List[Dict[str, Any]] = document.setdefault("cells", [])
        index = None
        for position, cell in enumerate(cells):
            if str(cell.get("id", "")) == cell_id or str(position) == cell_id:
                index = position
                break

        if edit_mode == "insert":
            cell: Dict[str, Any] = {
                "cell_type": cell_type,
                "id": uuid.uuid4().hex[:8],
                "metadata": {},
                "source": new_source.splitlines(keepends=True),
            }
            if cell_type == "code":
                cell["execution_count"] = None
                cell["outputs"] = []
            position = len(cells) if index is None else index + 1
            cells.insert(position, cell)
        else:
            if index is None:
                raise ValueError(f"cell {cell_id!r} was not found")
            if edit_mode == "delete":
                cells.pop(index)
                position = index
            else:
                cell = cells[index]
                cell["source"] = new_source.splitlines(keepends=True)
                cell["cell_type"] = cell_type
                if cell_type == "code":
                    cell.setdefault("execution_count", None)
                    cell.setdefault("outputs", [])
                else:
                    cell.pop("execution_count", None)
                    cell.pop("outputs", None)
                position = index

        path.write_text(json.dumps(document, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        return {
            "path": _relative(cfg, path),
            "edit_mode": edit_mode,
            "cell_index": position,
            "cell_count": len(cells),
        }

    def Glob(pattern: str, path: str = "", limit: int = 200) -> Dict[str, Any]:
        """Find files by name pattern inside the workspace.

        Args:
            pattern: Glob pattern such as "*.py" or "**/*.md".
            path: Directory to search from; empty means the workspace root.
            limit: Maximum number of paths to return (1-1000).
        """
        if Path(pattern).is_absolute():
            raise ValueError("pattern must be relative")
        base = _resolve(cfg, path or ".", must_exist=True)
        if not base.is_dir():
            raise ValueError(f"{path!r} is not a directory")

        matches = [item for item in base.glob(pattern) if item.is_file()]
        matches.sort(key=lambda item: item.stat().st_mtime, reverse=True)
        count = max(1, min(limit, 1000))
        return {
            "pattern": pattern,
            "matches": [_relative(cfg, item) for item in matches[:count]],
            "total": len(matches),
            "truncated": len(matches) > count,
        }

    def Grep(
        pattern: str,
        path: str = "",
        glob: str = "",
        output_mode: Literal["files_with_matches", "content", "count"] = "files_with_matches",
        case_insensitive: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        """Search file contents with a regular expression.

        Args:
            pattern: Python regular expression to search for.
            path: File or directory to search; empty means the workspace root.
            glob: Only search files whose name matches this glob.
            output_mode: Return matching files, matching lines, or per-file counts.
            case_insensitive: Match without regard to case.
            limit: Maximum results to return (1-1000).
        """
        base = _resolve(cfg, path or ".", must_exist=True)
        try:
            matcher = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        except re.error as exc:
            raise ValueError(f"invalid regular expression: {exc}") from exc

        count = max(1, min(limit, 1000))
        files = _collect_grep_files(cfg, base, glob)
        matches: List[Dict[str, Any]] = []
        counts: List[Dict[str, Any]] = []
        matched_files: List[str] = []

        for file_path in files:
            if file_path.stat().st_size > cfg.max_file_bytes:
                continue
            try:
                text = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            file_hits = 0
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not matcher.search(line):
                    continue
                file_hits += 1
                if output_mode == "content" and len(matches) < count:
                    matches.append(
                        {
                            "path": _relative(cfg, file_path),
                            "line": line_number,
                            "text": line[:500],
                        }
                    )
            if file_hits:
                matched_files.append(_relative(cfg, file_path))
                counts.append({"path": _relative(cfg, file_path), "count": file_hits})

        if output_mode == "content":
            return {"matches": matches, "truncated": len(matches) >= count}
        if output_mode == "count":
            return {
                "counts": counts[:count],
                "total_matches": sum(item["count"] for item in counts),
                "truncated": len(counts) > count,
            }
        return {
            "matches": matched_files[:count],
            "total": len(matched_files),
            "truncated": len(matched_files) > count,
        }

    def Bash(command: str, description: str = "", timeout_seconds: int = 120) -> Dict[str, Any]:
        """Run a shell command in the workspace.

        Args:
            command: Command line passed to `bash -lc`.
            description: Short note about what the command does.
            timeout_seconds: Timeout in seconds, capped by AGENT_TOOL_TIMEOUT_SECONDS.
        """
        return execute_command(cfg, command, description, timeout_seconds, shell="bash")

    def PowerShell(command: str, description: str = "", timeout_seconds: int = 120) -> Dict[str, Any]:
        """Run a PowerShell command in the workspace.

        Args:
            command: Command passed to `pwsh -NoProfile -NonInteractive -Command`.
            description: Short note about what the command does.
            timeout_seconds: Timeout in seconds, capped by AGENT_TOOL_TIMEOUT_SECONDS.
        """
        return execute_command(cfg, command, description, timeout_seconds, shell="powershell")

    # Per-run dedup cache: a repeated WebFetch returns the earlier result
    # instead of hitting the network again.
    fetch_cache: Dict[str, Dict[str, Any]] = {}
    # Repeated WebSearch queries reuse the earlier answer, and results already
    # returned or fetched are annotated so the model stops re-researching.
    search_cache: Dict[str, Dict[str, Any]] = {}
    seen_urls: Dict[str, str] = {}

    def WebFetch(url: str, full: bool = False) -> Dict[str, Any]:
        """Fetch a public URL and return its text content.

        Repeating a URL within the same run returns the cached result with
        ``cached: true`` instead of fetching the page again. A large cached page
        comes back as a short digest unless ``full`` is true.

        Args:
            url: HTTP or HTTPS URL. Private and loopback addresses are rejected unless explicitly allowed.
            full: Return the whole cached page instead of a digest when the URL was already fetched.
        """
        return _fetch_url(cfg, url, cache=fetch_cache, full=full)

    def WebSearch(query: str, max_results: int = DEFAULT_SEARCH_RESULTS) -> Dict[str, Any]:
        """Search the web and return result titles, URLs, and snippets.

        Use it to discover pages, then pass a result URL to WebFetch. Repeating
        a query within the same run returns the earlier results; results the run
        has already seen carry ``previously_seen`` or ``already_fetched``.

        Args:
            query: Search keywords in natural language.
            max_results: Maximum number of results to return (1-10).
        """
        return _search_web_scoped(
            cfg,
            query,
            max_results,
            cache=search_cache,
            seen_urls=seen_urls,
            fetched=fetch_cache,
        )

    def Monitor(
        action: Literal["start", "poll", "stop"],
        command: str = "",
        monitor_id: str = "",
        wait_seconds: float = 1.0,
        pattern: str = "",
    ) -> Dict[str, Any]:
        """Run or observe a long-lived command in the background.

        Args:
            action: Start a command, poll a running monitor, or stop it.
            command: Command line for action="start".
            monitor_id: Id returned by a previous start call.
            wait_seconds: How long to wait for output (0-30).
            pattern: Return early when an output line matches this regex.
        """
        return execute_monitor(cfg, action, command, monitor_id, wait_seconds, pattern)

    tools = [
        sdk.tool(Read, name="Read"),
        sdk.tool(Write, name="Write"),
        sdk.tool(Edit, name="Edit"),
        sdk.tool(NotebookEdit, name="NotebookEdit"),
        sdk.tool(Glob, name="Glob"),
        sdk.tool(Grep, name="Grep"),
        sdk.tool(WebFetch, name="WebFetch"),
        sdk.tool(WebSearch, name="WebSearch"),
    ]

    if cfg.office_tools:
        # Imported lazily: office_tools imports this module for path confinement.
        from app.agent.office_tools import build_office_tools

        tools.extend(build_office_tools(sdk, cfg))

    if cfg.allow_shell:
        tools.extend(
            [
                sdk.tool(Bash, name="Bash"),
                sdk.tool(PowerShell, name="PowerShell"),
                sdk.tool(Monitor, name="Monitor"),
            ]
        )
    return tools


def command_env(config: DevToolConfig) -> Dict[str, str]:
    """Environment for spawned commands: no API keys, DB credentials, or executor token."""
    home = str(_workspace_root(config))
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin",
        "HOME": home,
        "PWD": home,
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TERM": "dumb",
    }


def run_command(
    config: DevToolConfig,
    command: str,
    description: str,
    timeout_seconds: int,
    *,
    shell: str,
) -> Dict[str, Any]:
    if not command.strip():
        raise ValueError("command must not be empty")

    timeout = min(max(1.0, float(timeout_seconds)), config.command_timeout)
    if shell == "bash":
        executable = shutil.which("bash")
        if executable is None:
            raise RuntimeError("bash is not available in this container")
        argv = [executable, "-lc", command]
    else:
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            raise RuntimeError("PowerShell (pwsh) is not installed in this container")
        argv = [executable, "-NoProfile", "-NonInteractive", "-Command", command]

    try:
        completed = subprocess.run(
            argv,
            cwd=_workspace_root(config),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            env=command_env(config),
        )
        stdout, stdout_truncated = _truncate(completed.stdout or "", config.max_output_bytes)
        stderr, stderr_truncated = _truncate(completed.stderr or "", config.max_output_bytes)
        return {
            "command": command,
            "description": description,
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": False,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        truncated_stdout, stdout_truncated = _truncate(stdout, config.max_output_bytes)
        truncated_stderr, stderr_truncated = _truncate(stderr, config.max_output_bytes)
        return {
            "command": command,
            "description": description,
            "exit_code": None,
            "stdout": truncated_stdout,
            "stderr": truncated_stderr,
            "timed_out": True,
            "truncated": stdout_truncated or stderr_truncated,
            "timeout_seconds": timeout,
        }


def monitor_action(
    config: DevToolConfig,
    action: str,
    command: str,
    monitor_id: str,
    wait_seconds: float,
    pattern: str,
) -> Dict[str, Any]:
    wait = min(max(0.0, float(wait_seconds)), 30.0)

    if action == "start":
        if not command.strip():
            raise ValueError("command must not be empty for action='start'")
        process = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=_workspace_root(config),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
            start_new_session=True,
            env=command_env(config),
        )
        monitor = _Monitor(process, label=command)
        monitor_id = uuid.uuid4().hex[:12]
        with _MONITORS_LOCK:
            _MONITORS[monitor_id] = monitor
        lines, matched = monitor.read_new(wait, pattern)
        return {
            "monitor_id": monitor_id,
            "action": "start",
            "output": "\n".join(lines),
            "running": monitor.running,
            "exit_code": monitor.exit_code,
            "pattern_matched": matched,
        }

    if not monitor_id:
        raise ValueError("monitor_id is required for action='poll' and action='stop'")

    with _MONITORS_LOCK:
        monitor = _MONITORS.get(monitor_id)

    if monitor is None:
        raise ValueError(f"unknown monitor_id {monitor_id!r}")

    if action == "poll":
        lines, matched = monitor.read_new(wait, pattern)
        return {
            "monitor_id": monitor_id,
            "action": "poll",
            "output": "\n".join(lines),
            "running": monitor.running,
            "exit_code": monitor.exit_code,
            "pattern_matched": matched,
        }

    monitor.terminate()
    lines, matched = monitor.read_new(0.0, "")
    with _MONITORS_LOCK:
        _MONITORS.pop(monitor_id, None)
    return {
        "monitor_id": monitor_id,
        "action": "stop",
        "output": "\n".join(lines),
        "running": False,
        "exit_code": monitor.process.poll(),
        "pattern_matched": matched,
    }
