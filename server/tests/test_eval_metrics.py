from types import SimpleNamespace

from app.eval.metrics import _available_tools_from_trace, metric_names


class _FakeMetric:
    threshold = 0.5

    @property
    def __name__(self):
        return "Fake Metric"


def test_metric_names_use_the_reported_label():
    assert metric_names([_FakeMetric()]) == {"Fake Metric": 0.5}


def test_available_tools_are_collected_from_metadata_and_the_trace_tree():
    case = SimpleNamespace(
        metadata={"available_tools": ["Read", "WebSearch"]},
        _trace_dict={
            "type": "agent",
            "available_tools": ["Read", "Bash"],
            "children": [{"type": "tool", "name": "Bash"}],
        },
    )

    assert _available_tools_from_trace(case) == {"Read", "WebSearch", "Bash"}


def test_available_tools_is_empty_without_a_trace():
    assert _available_tools_from_trace(SimpleNamespace()) == set()
