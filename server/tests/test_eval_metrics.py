from app.eval.metrics import metric_names


class _FakeMetric:
    threshold = 0.5

    @property
    def __name__(self):
        return "Fake Metric"


def test_metric_names_use_the_reported_label():
    assert metric_names([_FakeMetric()]) == {"Fake Metric": 0.5}
