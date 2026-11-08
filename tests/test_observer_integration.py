"""Prueba de integración real contra CloudWatch.

Ejecutar con:
    TEST_INSTANCE_ID=i-xxxx pytest -m integration tests/test_observer_integration.py
"""

import os
from datetime import timedelta
import pytest
import boto3

from controllerasg.configuration import Dimension, MetricPolicy, ScalingConfig
from controllerasg.model import Instance
from controllerasg.observer import CloudWatchObserver


@pytest.mark.integration
def test_integration_ec2_cpu():
    instance_id = os.getenv("TEST_INSTANCE_ID")
    if not instance_id:
        pytest.skip("define TEST_INSTANCE_ID para correr esta prueba")

    region = os.getenv("AWS_REGION", "us-east-1")
    period = int(os.getenv("TEST_PERIOD", "300"))
    window_min = int(os.getenv("TEST_WINDOW_MIN", "15"))

    session = boto3.Session(region_name=region)
    cw = session.client("cloudwatch")

    metrics = [
        MetricPolicy(
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            dimensions=[Dimension(name="InstanceId", value=instance_id)],
            period=period,
            stat="Average",
            scaling=ScalingConfig(scale_up_threshold=70, scale_down_threshold=20),
        )
    ]

    o = CloudWatchObserver(cw, metrics, timedelta(minutes=window_min))
    got = o.observe([])
    assert len(got) > 0, "CloudWatch respondió sin datos"
    for m in got:
        print(f"{m.name} = {m.value:.2f} (dato más reciente: {m.timestamp.isoformat()})")


@pytest.mark.integration
def test_integration_per_instance_cpu():
    instance_id = os.getenv("TEST_INSTANCE_ID")
    if not instance_id:
        pytest.skip("define TEST_INSTANCE_ID para correr esta prueba")

    region = os.getenv("AWS_REGION", "us-east-1")
    period = int(os.getenv("TEST_PERIOD", "300"))
    window_min = int(os.getenv("TEST_WINDOW_MIN", "15"))

    session = boto3.Session(region_name=region)
    cw = session.client("cloudwatch")

    metrics = [
        MetricPolicy(
            namespace="AWS/EC2",
            metric_name="CPUUtilization",
            per_instance=True,
            period=period,
            stat="Average",
            scaling=ScalingConfig(scale_up_threshold=70, scale_down_threshold=20),
        )
    ]

    o = CloudWatchObserver(cw, metrics, timedelta(minutes=window_min))
    got = o.observe([Instance(id=instance_id)])
    assert len(got) > 0, "CloudWatch respondió sin datos para métrica per-instance"
    print(f"{got[0].name} (per-instance) = {got[0].value:.2f}")
