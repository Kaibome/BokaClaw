"""Contract tests for the span -> deepeval adapter.

These tests need deepeval 4.3.3 installed in the evaluation environment (the
``eval`` extra). They skip when deepeval is absent or the installed version
differs from the pin, because the adapter deliberately fails loudly in that
case.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_EVAL_DIR = Path(__file__).resolve().parent
if str(_EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(_EVAL_DIR))

import adapter  # noqa: E402

try:
    import deepeval
    from deepeval.tracing import trace_manager
    from deepeval.tracing.types import AgentSpan, LlmSpan, ToolSpan, TraceSpanStatus
except Exception as exc:  # pragma: no cover - depends on the eval environment
    pytest.skip(f"deepeval is not importable here: {exc}", allow_module_level=True)

if getattr(deepeval, "__version__", None) != adapter.DEEPEVAL_VERSION:  # pragma: no cover
    pytest.skip(
        f"deepeval {adapter.DEEPEVAL_VERSION} required, found {getattr(deepeval, '__version__', None)!r}",
        allow_module_level=True,
    )


def _span(span_id, span_type, name, *, parent=None, input=None, output=None, attributes=None, error=None):
    return SimpleNamespace(
        span_id=span_id,
        parent_span_id=parent,
        span_type=span_type,
        name=name,
        input=input,
        output=output,
        attributes=attributes or {},
        error=error,
    )


def _run():
    return SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        status="completed",
        request_payload={
            "message": "Find the answer",
            "metadata": {"eval_case_id": "case-1"},
        },
    )


def _span_rows():
    return [
        _span(
            "agent",
            "agent",
            "agent.turn",
            input={"message": "Find the answer"},
            output={"content": "the answer"},
            attributes={"tool_names": ["search"]},
        ),
        _span(
            "llm:10",
            "llm",
            "llm.turn.1",
            parent="agent",
            input={"summary": {"message_count": 2, "roles": ["system", "user"]}},
            output={"content": "the answer", "finish_reason": "stop"},
            attributes={"model": "gpt-4o-mini", "usage": {"prompt_tokens": 12, "completion_tokens": 3}},
        ),
        _span(
            "tool:t1",
            "tool",
            "search",
            parent="agent",
            input={"arguments": {"query": "answer"}},
            output={"content": "found it"},
        ),
    ]


def _real_trace_dict():
    """Build the same tree through deepeval's public span classes."""
    llm = LlmSpan(
        uuid="llm:10",
        trace_uuid="trace",
        status=TraceSpanStatus.SUCCESS,
        name="llm.turn.1",
        start_time=time.perf_counter(),
        input={"summary": {"message_count": 2, "roles": ["system", "user"]}},
        output={"content": "the answer"},
        model="gpt-4o-mini",
        input_token_count=12,
        output_token_count=3,
    )
    tool = ToolSpan(
        uuid="tool:t1",
        trace_uuid="trace",
        status=TraceSpanStatus.SUCCESS,
        name="search",
        start_time=time.perf_counter(),
        input={"arguments": {"query": "answer"}},
        output={"content": "found it"},
    )
    agent = AgentSpan(
        uuid="agent",
        trace_uuid="trace",
        status=TraceSpanStatus.SUCCESS,
        name="agent.turn",
        start_time=time.perf_counter(),
        input={"message": "Find the answer"},
        output={"content": "the answer"},
        available_tools=["search"],
        children=[llm, tool],
    )
    return trace_manager.create_nested_spans_dict(agent)


def test_trace_dict_shape_matches_deepeval():
    ours = adapter.build_trace_dict(_span_rows())
    real = _real_trace_dict()

    assert set(ours) <= set(real), f"unexpected keys: {set(ours) - set(real)}"
    assert ours["type"] == real["type"] == "agent"
    assert ours["name"] == real["name"]
    assert ours["input"] == real["input"]
    assert ours["output"] == real["output"]

    our_children = {child["name"]: child for child in ours["children"]}
    real_children = {child["name"]: child for child in real["children"]}
    assert set(our_children) == set(real_children)
    for name, child in our_children.items():
        assert set(child) <= set(real_children[name]), f"unexpected keys under {name}"
        assert child["type"] == real_children[name]["type"]


def test_build_trace_dict_links_by_parent_and_synthesizes_root():
    rows = [_span("a", "llm", "llm.turn.1"), _span("b", "tool", "search")]
    tree = adapter.build_trace_dict(rows)

    assert tree["type"] == "agent"
    assert [child["name"] for child in tree["children"]] == ["llm.turn.1", "search"]

    linked = adapter.build_trace_dict(
        [
            _span("root", "agent", "agent.turn"),
            _span("a", "llm", "llm.turn.1", parent="root"),
        ]
    )
    assert [child["name"] for child in linked["children"]] == ["llm.turn.1"]


def test_to_llm_test_case_maps_run_and_tools():
    case = adapter.to_llm_test_case(_span_rows(), run=_run())

    assert case.input == "Find the answer"
    assert case.actual_output == "the answer"
    assert [tool.name for tool in case.tools_called] == ["search"]
    assert case.tools_called[0].input_parameters == {"query": "answer"}
    assert case.tools_called[0].output == "found it"
    assert case.metadata["run_id"] == "11111111-1111-1111-1111-111111111111"
    assert case.metadata["eval_case_id"] == "case-1"
    assert case._trace_dict["type"] == "agent"


def test_require_deepeval_accepts_the_pinned_version():
    assert adapter.require_deepeval().__version__ == adapter.DEEPEVAL_VERSION
