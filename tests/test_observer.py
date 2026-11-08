"""Pruebas unitarias para el módulo observer."""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from controllerasg.configuration import Dimension, MetricPolicy, ScalingConfig
from controllerasg.model import Instance
from controllerasg.observer import CloudWatchObserver


class FakeClient:
    """Cliente simulado de CloudWatch que devuelve páginas predefinidas."""

    def __init__(self, pages: Optional[list[dict[str, Any]]] = None, err: Optional[Exception] = None) -> None:
        self.pages = pages or [{}]
        self.err = err
        self.calls = 0
        self.last_input: Optional[dict[str, Any]] = None

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.last_input = kwargs
        if self.err:
            raise self.err
        page = self.pages[self.calls]
        self.calls += 1
        return page


def sample_metrics() -> list[MetricPolicy]:
    dims = [Dimension(name="TargetGroup", value="targetgroup/tg/1")]
    return [
        MetricPolicy(
            namespace="AWS/ApplicationELB",
            metric_name="RequestCountPerTarget",
            dimensions=dims,
            period=60,
            stat="Sum",
            scaling=ScalingConfig(scale_up_threshold=1000, scale_down_threshold=200),
        ),
        MetricPolicy(
            namespace="AWS/ApplicationELB",
            metric_name="TargetResponseTime",
            dimensions=dims,
            period=60,
            stat="Average",
            scaling=ScalingConfig(scale_up_threshold=0.5, scale_down_threshold=0.1),
        ),
    ]


def cpu_metric() -> MetricPolicy:
    return MetricPolicy(
        namespace="AWS/EC2",
        metric_name="CPUUtilization",
        per_instance=True,
        period=60,
        stat="Average",
        scaling=ScalingConfig(scale_up_threshold=70, scale_down_threshold=20),
    )


def make_result(qid: str, *values: float, status_code: str = "Complete") -> dict[str, Any]:
    base_time = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)
    ts = [base_time - timedelta(minutes=i) for i in range(len(values))]
    return {
        "Id": qid,
        "Values": list(values),
        "Timestamps": ts,
        "StatusCode": status_code,
    }


def test_build_queries():
    o = CloudWatchObserver(FakeClient(), sample_metrics(), timedelta(minutes=5))
    qs, ids = o.build_queries([])

    assert len(qs) == 2
    assert ids[0] == ["m0"]
    assert ids[1] == ["m1"]

    q1 = qs[1]
    assert q1["Id"] == "m1"
    stat = q1["MetricStat"]
    assert stat["Metric"]["Namespace"] == "AWS/ApplicationELB"
    assert stat["Metric"]["MetricName"] == "TargetResponseTime"
    assert stat["Metric"]["Dimensions"] == [{"Name": "TargetGroup", "Value": "targetgroup/tg/1"}]
    assert stat["Period"] == 60
    assert stat["Stat"] == "Average"


def test_build_queries_per_instance():
    o = CloudWatchObserver(FakeClient(), [cpu_metric()], timedelta(minutes=5))
    fleet = [Instance(id="i-aaa"), Instance(id="i-bbb")]

    qs, ids = o.build_queries(fleet)

    assert len(qs) == 2
    assert len(ids[0]) == 2
    seen = {q["Id"]: q for q in qs}
    assert "m0_iaaa" in seen
    assert "m0_ibbb" in seen
    assert seen["m0_iaaa"]["MetricStat"]["Metric"]["Dimensions"] == [{"Name": "InstanceId", "Value": "i-aaa"}]


def test_observe_averages_window():
    client = FakeClient(
        pages=[
            {
                "MetricDataResults": [
                    make_result("m0", 100, 200, 300),
                    make_result("m1", 0.2, 0.4),
                ]
            }
        ]
    )
    o = CloudWatchObserver(client, sample_metrics(), timedelta(minutes=5))

    got = o.observe([])

    assert len(got) == 2
    assert got[0].name == "RequestCountPerTarget"
    assert got[0].value == 200.0
    assert pytest.approx(got[1].value, 0.01) == 0.3
    assert got[0].timestamp is not None
    assert client.last_input is not None
    window = client.last_input["EndTime"] - client.last_input["StartTime"]
    assert pytest.approx(window.total_seconds(), 1.0) == 300


def test_observe_per_instance_averages_across_fleet():
    client = FakeClient(
        pages=[
            {
                "MetricDataResults": [
                    make_result("m0_iaaa", 80, 90),  # avg: 85
                    make_result("m0_ibbb", 20, 30),  # avg: 25
                ]
            }
        ]
    )
    o = CloudWatchObserver(client, [cpu_metric()], timedelta(minutes=5))
    fleet = [Instance(id="i-aaa"), Instance(id="i-bbb")]

    got = o.observe(fleet)

    assert len(got) == 1
    assert got[0].name == "CPUUtilization"
    assert got[0].value == 55.0  # promedio de (85, 25)


def test_observe_per_instance_ignores_instances_without_data():
    client = FakeClient(
        pages=[
            {
                "MetricDataResults": [
                    make_result("m0_iaaa", 60),
                    make_result("m0_ibbb"),  # sin datapoints
                ]
            }
        ]
    )
    o = CloudWatchObserver(client, [cpu_metric()], timedelta(minutes=5))
    fleet = [Instance(id="i-aaa"), Instance(id="i-bbb")]

    got = o.observe(fleet)

    assert len(got) == 1
    assert got[0].value == 60.0


def test_observe_per_instance_omitted_when_no_instance_has_data():
    client = FakeClient(
        pages=[
            {
                "MetricDataResults": [
                    make_result("m0_iaaa"),
                    make_result("m0_ibbb"),
                ]
            }
        ]
    )
    o = CloudWatchObserver(client, [cpu_metric()], timedelta(minutes=5))
    fleet = [Instance(id="i-aaa"), Instance(id="i-bbb")]

    got = o.observe(fleet)
    assert len(got) == 0


def test_observe_mixes_static_and_per_instance_metrics():
    client = FakeClient(
        pages=[
            {
                "MetricDataResults": [
                    make_result("m0", 500),
                    make_result("m1_iaaa", 40),
                ]
            }
        ]
    )
    metrics = [sample_metrics()[0], cpu_metric()]
    o = CloudWatchObserver(client, metrics, timedelta(minutes=5))
    fleet = [Instance(id="i-aaa")]

    got = o.observe(fleet)

    assert len(got) == 2
    by_name = {m.name: m.value for m in got}
    assert by_name["RequestCountPerTarget"] == 500
    assert by_name["CPUUtilization"] == 40


def test_observe_omits_metrics_without_data():
    client = FakeClient(
        pages=[
            {
                "MetricDataResults": [
                    make_result("m0", 100),
                    make_result("m1"),
                ]
            }
        ]
    )
    o = CloudWatchObserver(client, sample_metrics(), timedelta(minutes=5))

    got = o.observe([])

    assert len(got) == 1
    assert got[0].name == "RequestCountPerTarget"


def test_observe_no_data_at_all():
    client = FakeClient(pages=[{}])
    o = CloudWatchObserver(client, sample_metrics(), timedelta(minutes=5))

    got = o.observe([])
    assert len(got) == 0


def test_observe_merges_pages():
    client = FakeClient(
        pages=[
            {"MetricDataResults": [make_result("m0", 100)], "NextToken": "siguiente"},
            {"MetricDataResults": [make_result("m0", 300)]},
        ]
    )
    o = CloudWatchObserver(client, sample_metrics()[:1], timedelta(minutes=5))

    got = o.observe([])

    assert client.calls == 2
    assert len(got) == 1
    assert got[0].value == 200.0


def test_observe_propagates_aws_error():
    boom = RuntimeError("throttled")
    o = CloudWatchObserver(FakeClient(err=boom), sample_metrics(), timedelta(minutes=5))

    with pytest.raises(RuntimeError) as excinfo:
        o.observe([])
    assert "throttled" in str(excinfo.value)


def test_observe_forbidden():
    forbidden = make_result("m0", 100, status_code="Forbidden")
    client = FakeClient(pages=[{"MetricDataResults": [forbidden]}])
    o = CloudWatchObserver(client, sample_metrics(), timedelta(minutes=5))

    with pytest.raises(RuntimeError) as excinfo:
        o.observe([])
    assert "acceso denegado" in str(excinfo.value)
