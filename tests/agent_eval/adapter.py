"""Turn persisted ``agent_run_spans`` rows into deepeval test cases.

Persisted spans are neutral. This module is the only place that knows about
deepeval, including its pinned version and the private ``_trace_dict`` slot
used by the trajectory metrics. Keep it that way: the capture side must not
import deepeval.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

DEEPEVAL_VERSION = "4.3.3"


def require_deepeval() -> Any:
    """Import deepeval and fail loudly on an unpinned version."""
    import deepeval

    version = getattr(deepeval, "__version__", None)
    if version != DEEPEVAL_VERSION:
        raise RuntimeError(
            f"deepeval {DEEPEVAL_VERSION} is required for the span adapter; found {version!r}. "
            "Install the eval extra in an isolated environment."
        )
    return deepeval


def _attr(span: Any, name: str, default: Any = None) -> Any:
    if isinstance(span, dict):
        return span.get(name, default)
    return getattr(span, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "message", "text", "output"):
            if isinstance(value.get(key), str):
                return value[key]
    return json.dumps(value, ensure_ascii=False, default=str)


def _root_span(spans: Iterable[Any]) -> Any | None:
    for span in spans:
        if _attr(span, "span_type") == "agent":
            return span
    return None


def _tool_calls(spans: Iterable[Any]) -> list[Any]:
    from deepeval.test_case import ToolCall

    calls = []
    for span in spans:
        if _attr(span, "span_type") != "tool":
            continue
        tool_input = _attr(span, "input") or {}
        tool_output = _attr(span, "output") or {}
        arguments = tool_input.get("arguments") if isinstance(tool_input, dict) else None
        output = tool_output.get("content") if isinstance(tool_output, dict) else tool_output
        calls.append(
            ToolCall(
                name=_attr(span, "name") or "tool",
                input_parameters=arguments if isinstance(arguments, dict) else None,
                output=output,
            )
        )
    return calls


def span_to_trace_dict(span: Any) -> dict[str, Any]:
    """Build one node shaped like deepeval's ``create_nested_spans_dict`` output.

    deepeval strips timing/status/UUID from that dict, so this carries only the
    fields the trajectory metrics read: type, name, input, output, error and
    type-specific attributes.
    """
    span_type = _attr(span, "span_type") or "agent"
    attributes = _attr(span, "attributes") or {}
    node: dict[str, Any] = {
        "type": span_type,
        "name": _attr(span, "name") or _attr(span, "span_id") or span_type,
        "children": [],
    }
    input_value = _attr(span, "input")
    output_value = _attr(span, "output")
    if input_value:
        node["input"] = input_value
    if output_value:
        node["output"] = output_value
    error = _attr(span, "error")
    if error:
        node["error"] = error
    if span_type == "llm":
        if attributes.get("model"):
            node["model"] = attributes["model"]
        if attributes.get("provider"):
            node["provider"] = attributes["provider"]
        usage = attributes.get("usage")
        if isinstance(usage, dict):
            if usage.get("prompt_tokens") is not None:
                node["input_token_count"] = usage["prompt_tokens"]
            if usage.get("completion_tokens") is not None:
                node["output_token_count"] = usage["completion_tokens"]
    elif span_type == "agent":
        node["available_tools"] = attributes.get("tool_names") or []
        node["agent_handoffs"] = []
    return node


def build_trace_dict(spans: Iterable[Any]) -> dict[str, Any]:
    """Assemble the span rows into one nested trace dictionary."""
    ordered = list(spans)
    nodes = {_attr(span, "span_id"): span_to_trace_dict(span) for span in ordered}
    roots: list[dict[str, Any]] = []
    for span in ordered:
        node = nodes[_attr(span, "span_id")]
        parent_id = _attr(span, "parent_span_id")
        parent = nodes.get(parent_id) if parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent["children"].append(node)
    if len(roots) == 1:
        return roots[0]
    return {"type": "agent", "name": "agent.trace", "children": roots}


def to_llm_test_case(spans: Iterable[Any], *, run: Any = None, golden: Any = None) -> Any:
    """Convert one run's span tree into a public ``LLMTestCase``."""
    from deepeval.test_case import LLMTestCase

    ordered = list(spans)
    root = _root_span(ordered)
    input_text = ""
    output_text = ""
    if root is not None:
        root_input = _attr(root, "input") or {}
        root_output = _attr(root, "output") or {}
        input_text = _text(root_input.get("message") if isinstance(root_input, dict) else root_input)
        output_text = _text(root_output.get("content") if isinstance(root_output, dict) else root_output)
    if not input_text and run is not None:
        payload = _attr(run, "request_payload") or {}
        input_text = _text(payload.get("message"))

    case = LLMTestCase(
        input=input_text,
        actual_output=output_text,
        expected_output=_attr(golden, "expected_output") if golden is not None else None,
        expected_tools=_attr(golden, "expected_tools") if golden is not None else None,
        tools_called=_tool_calls(ordered) or None,
        metadata={
            "run_id": str(_attr(run, "id")) if run is not None else None,
            "status": _attr(run, "status") if run is not None else None,
            "eval_case_id": (
                ((_attr(run, "request_payload") or {}).get("metadata") or {}).get("eval_case_id")
                if run is not None
                else None
            ),
        },
    )
    # deepeval 4.3.3 has no public trace-import API. This is the single private
    # touch point; the contract test in test_deepeval_adapter.py guards it.
    case._trace_dict = build_trace_dict(ordered)
    return case


def load_run_spans(run_id: Any, repo: Any = None) -> list[Any]:
    if repo is None:
        from app.agent.repo import RunRepository

        repo = RunRepository()
    return repo.list_spans(run_id)


def load_run(run_id: Any, repo: Any = None) -> Any:
    if repo is None:
        from app.agent.repo import RunRepository

        repo = RunRepository()
    return repo.get_run(run_id)
