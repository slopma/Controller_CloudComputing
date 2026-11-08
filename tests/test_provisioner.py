"""Pruebas unitarias para el aprovisionador de infraestructura (Provisioner)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from controllerasg.configuration import (
    ComputeConfig,
    Config,
    LoadBalancerConfig,
)
from controllerasg.model import Instance
from controllerasg.provisioner import (
    HEALTH_DRAINING,
    HEALTH_HEALTHY,
    HEALTH_INITIAL,
    HEALTH_UNHEALTHY,
    HEALTH_UNREGISTERED,
    HEALTH_UNUSED,
    MANAGED_TAG_KEY,
    MANAGED_TAG_VALUE,
    Provisioner,
    pick_subnet,
    pick_victim,
    warming_up,
)

T0 = datetime(2026, 9, 20, 12, 0, 0, tzinfo=timezone.utc)


class FakeEC2:
    def __init__(self, instances: Optional[list[dict[str, Any]]] = None, events: Optional[list[str]] = None) -> None:
        self.instances_list = instances or []
        self.run_err: Optional[Exception] = None
        self.launched: Optional[dict[str, Any]] = None
        self.terminated: list[str] = []
        self.events = events if events is not None else []

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.launched = kwargs
        if self.run_err:
            raise self.run_err
        return {"Instances": [{"InstanceId": "i-new"}]}

    def terminate_instances(self, **kwargs: Any) -> dict[str, Any]:
        ids = kwargs.get("InstanceIds", [])
        self.terminated.extend(ids)
        self.events.append("terminate")
        return {}

    def describe_instances(self, **kwargs: Any) -> dict[str, Any]:
        ids = kwargs.get("InstanceIds", [])
        if ids:
            return {
                "Reservations": [
                    {
                        "Instances": [
                            {"InstanceId": ids[0], "State": {"Name": "running"}}
                        ]
                    }
                ]
            }
        return {"Reservations": [{"Instances": self.instances_list}]}


class FakeELB:
    def __init__(self, health: Optional[dict[str, str]] = None, events: Optional[list[str]] = None) -> None:
        self.health = health or {}
        self.registered: list[str] = []
        self.register_err: Optional[Exception] = None
        self.deregistered: list[str] = []
        self.drain_checks = 0
        self.events = events if events is not None else []

    def register_targets(self, **kwargs: Any) -> dict[str, Any]:
        if self.register_err:
            raise self.register_err
        for t in kwargs.get("Targets", []):
            self.registered.append(t.get("Id", ""))
        return {}

    def deregister_targets(self, **kwargs: Any) -> dict[str, Any]:
        for t in kwargs.get("Targets", []):
            tid = t.get("Id", "")
            self.deregistered.append(tid)
            self.health[tid] = HEALTH_DRAINING
        self.events.append("deregister")
        return {}

    def describe_target_health(self, **kwargs: Any) -> dict[str, Any]:
        descriptions = []
        for tid, state in list(self.health.items()):
            if state == HEALTH_DRAINING:
                self.drain_checks += 1
                if self.drain_checks >= 2:
                    self.health[tid] = HEALTH_UNUSED
                    state = HEALTH_UNUSED
            descriptions.append({
                "Target": {"Id": tid},
                "TargetHealth": {"State": state},
            })
        return {"TargetHealthDescriptions": descriptions}


def make_inst(inst_id: str, launched: datetime, subnet: str = "subnet-a") -> dict[str, Any]:
    return {
        "InstanceId": inst_id,
        "LaunchTime": launched,
        "PrivateIpAddress": "10.0.0.7",
        "SubnetId": subnet,
        "State": {"Name": "running"},
    }


def setup_provisioner(
    instances: Optional[list[dict[str, Any]]] = None,
    health: Optional[dict[str, str]] = None,
) -> tuple[Provisioner, FakeEC2, FakeELB, list[str]]:
    events: list[str] = []
    e = FakeEC2(instances=instances, events=events)
    l = FakeELB(health=health, events=events)
    cfg = Config(
        compute=ComputeConfig(
            launch_template_id="lt-1",
            launch_template_version="$Latest",
            min_instances=1,
            max_instances=3,
            subnet_ids=["subnet-a", "subnet-b"],
        ),
        load_balancer=LoadBalancerConfig(target_group_arn="arn:tg", target_port=80),
    )
    p = Provisioner(e, l, cfg, running_timeout=1.0, drain_timeout=2.0, drain_poll=0.01)
    return p, e, l, events


def test_instances_merges_health_and_marks_unregistered():
    p, _, _, _ = setup_provisioner(
        instances=[make_inst("i-a", T0), make_inst("i-b", T0)],
        health={"i-a": HEALTH_HEALTHY},
    )

    fleet = p.instances()

    got = {i.id: i.health for i in fleet}
    assert got["i-a"] == HEALTH_HEALTHY
    assert got["i-b"] == HEALTH_UNREGISTERED
    assert fleet[0].ip == "10.0.0.7"
    assert fleet[0].state == "running"


def test_scale_up_launches_tags_and_registers():
    p, e, l, _ = setup_provisioner(
        instances=[make_inst("i-a", T0)],
        health={"i-a": HEALTH_HEALTHY},
    )

    inst_id = p.scale_up()

    assert inst_id == "i-new"
    assert e.launched["LaunchTemplate"]["LaunchTemplateId"] == "lt-1"
    tags = e.launched["TagSpecifications"][0]["Tags"][0]
    assert tags["Key"] == MANAGED_TAG_KEY
    assert tags["Value"] == MANAGED_TAG_VALUE
    assert l.registered == ["i-new"]
    assert e.launched["SubnetId"] == "subnet-b"  # Distribución de subred


def test_pick_subnet_spreads_across_zones():
    subnets = ["subnet-a", "subnet-b"]
    fleet = [
        Instance(id="1", subnet_id="subnet-a"),
        Instance(id="2", subnet_id="subnet-a"),
        Instance(id="3", subnet_id="subnet-b"),
    ]
    assert pick_subnet(subnets, fleet) == "subnet-b"
    assert pick_subnet(subnets, []) == "subnet-a"


def test_scale_up_refuses_at_max():
    fleet = [make_inst("i-a", T0), make_inst("i-b", T0), make_inst("i-c", T0)]
    p, e, _, _ = setup_provisioner(instances=fleet)

    with pytest.raises(RuntimeError) as excinfo:
        p.scale_up()
    assert "el máximo es 3" in str(excinfo.value)
    assert e.launched is None


def test_scale_up_rolls_back_when_register_fails():
    p, e, l, _ = setup_provisioner(instances=[make_inst("i-a", T0)])
    l.register_err = RuntimeError("ELB connection failed")

    with pytest.raises(RuntimeError):
        p.scale_up()

    assert e.terminated == ["i-new"]


def test_scale_up_propagates_run_error():
    p, e, l, _ = setup_provisioner(instances=[make_inst("i-a", T0)])
    e.run_err = RuntimeError("InsufficientInstanceCapacity")

    with pytest.raises(RuntimeError) as excinfo:
        p.scale_up()
    assert "InsufficientInstanceCapacity" in str(excinfo.value)
    assert len(l.registered) == 0


def test_scale_down_deregisters_drains_then_terminates():
    p, e, l, events = setup_provisioner(
        instances=[
            make_inst("i-old", T0),
            make_inst("i-new", T0 + timedelta(hours=1)),
        ],
        health={"i-old": HEALTH_HEALTHY, "i-new": HEALTH_HEALTHY},
    )

    victim_id = p.scale_down()

    assert victim_id == "i-new"
    assert l.deregistered == ["i-new"]
    assert l.drain_checks >= 2
    assert e.terminated == ["i-new"]
    assert events == ["deregister", "terminate"]


def test_scale_down_prefers_unhealthy():
    p, e, _, _ = setup_provisioner(
        instances=[
            make_inst("i-bad", T0),
            make_inst("i-good", T0 + timedelta(hours=1)),
        ],
        health={"i-bad": HEALTH_UNHEALTHY, "i-good": HEALTH_HEALTHY},
    )

    victim_id = p.scale_down()
    assert victim_id == "i-bad"
    assert e.terminated == ["i-bad"]


def test_scale_down_refuses_at_min():
    p, e, _, _ = setup_provisioner(
        instances=[make_inst("i-a", T0)],
        health={"i-a": HEALTH_HEALTHY},
    )

    with pytest.raises(RuntimeError) as excinfo:
        p.scale_down()
    assert "el mínimo es 1" in str(excinfo.value)
    assert len(e.terminated) == 0


def test_scale_down_refuses_while_instance_warming_up():
    p, e, l, _ = setup_provisioner(
        instances=[
            make_inst("i-a", T0),
            make_inst("i-b", T0 + timedelta(hours=1)),
        ],
        health={"i-a": HEALTH_HEALTHY, "i-b": HEALTH_INITIAL},
    )

    with pytest.raises(RuntimeError) as excinfo:
        p.scale_down()
    assert "aún se está inicializando" in str(excinfo.value)
    assert len(e.terminated) == 0
    assert len(l.deregistered) == 0


def test_scale_down_skips_deregister_for_unregistered():
    p, e, l, _ = setup_provisioner(
        instances=[
            make_inst("i-a", T0),
            make_inst("i-orphan", T0 + timedelta(hours=1)),
        ],
        health={"i-a": HEALTH_HEALTHY},
    )

    victim_id = p.scale_down()

    assert victim_id == "i-orphan"
    assert len(l.deregistered) == 0
    assert e.terminated == ["i-orphan"]
