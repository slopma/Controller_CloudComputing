"""Pruebas unitarias para el controlador de auto-scaling."""

from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

import pytest

from controllerasg.configuration import (
    ComputeConfig,
    Config,
    MetricPolicy,
    ScalingConfig,
)
from controllerasg.controller import Controller
from controllerasg.model import (
    Capacity,
    Decision,
    DecisionRecord,
    Instance,
    ObservedMetric,
)


class FakeMetrics:
    def __init__(self, values: Optional[list[ObservedMetric]] = None, err: Optional[Exception] = None) -> None:
        self.values = values or []
        self.err = err

    def observe(self, fleet: Sequence[Instance]) -> list[ObservedMetric]:
        if self.err:
            raise self.err
        return self.values


class FakeFleet:
    def __init__(self, instances: Optional[list[Instance]] = None, err: Optional[Exception] = None) -> None:
        self.instances_list = instances or []
        self.err = err

    def instances(self) -> list[Instance]:
        if self.err:
            raise self.err
        return self.instances_list


class FakeActuator:
    def __init__(self, err: Optional[Exception] = None) -> None:
        self.err = err
        self.ups = 0
        self.downs = 0

    def scale_up(self) -> str:
        self.ups += 1
        if self.err:
            raise self.err
        return "i-new"

    def scale_down(self) -> str:
        self.downs += 1
        if self.err:
            raise self.err
        return "i-old"


class MemLogger:
    def __init__(self) -> None:
        self.recs: list[DecisionRecord] = []

    def log(self, rec: DecisionRecord) -> None:
        self.recs.append(rec)


def make_config() -> Config:
    return Config(
        cooldown_seconds=300,
        observation_window_seconds=300,
        confirm_cycles_down=1,
        scale_up_step=1,
        scale_down_step=1,
        compute=ComputeConfig(min_instances=1, max_instances=3),
        metrics=[
            MetricPolicy(
                metric_name="load",
                scaling=ScalingConfig(scale_up_threshold=70, scale_down_threshold=20),
            )
        ],
    )


def healthy_fleet(n: int) -> list[Instance]:
    return [
        Instance(
            id=f"i-{chr(ord('a') + i)}",
            state="running",
            health="healthy",
        )
        for i in range(n)
    ]


def load_metrics(val: float) -> FakeMetrics:
    return FakeMetrics(
        values=[
            ObservedMetric(
                name="load",
                value=val,
                timestamp=datetime.now(timezone.utc),
            )
        ]
    )


def run_tick(m: FakeMetrics, f: FakeFleet, a: FakeActuator) -> tuple[Controller, DecisionRecord]:
    l = MemLogger()
    c = Controller(make_config(), m, f, a, l)
    c.tick()
    return c, l.recs[0]


def test_scale_up_on_high_load():
    a = FakeActuator()
    _, rec = run_tick(load_metrics(90), FakeFleet(healthy_fleet(1)), a)

    assert rec.decision == Decision.INCREASE
    assert a.ups == 1
    assert rec.action == "ScaleUp"
    assert rec.result == "ok"
    assert rec.instance_id == "i-new"
    assert rec.capacity_before.total == 1
    assert rec.capacity_before.healthy == 1
    assert len(rec.metrics) == 1
    assert rec.window_seconds == 300


def test_scale_down_on_low_load():
    a = FakeActuator()
    _, rec = run_tick(load_metrics(5), FakeFleet(healthy_fleet(2)), a)

    assert rec.decision == Decision.REDUCE
    assert a.downs == 1
    assert rec.instance_id == "i-old"


def test_scale_down_ignores_missing_metric_that_went_quiet():
    cfg = make_config()
    cfg.metrics.append(
        MetricPolicy(
            metric_name="requests",
            scaling=ScalingConfig(scale_up_threshold=1000, scale_down_threshold=200),
        )
    )
    a = FakeActuator()
    l = MemLogger()
    c = Controller(cfg, load_metrics(5), FakeFleet(healthy_fleet(2)), a, l)
    c.tick()

    rec = l.recs[0]
    assert rec.decision == Decision.REDUCE
    assert a.downs == 1


def test_maintain_between_thresholds():
    a = FakeActuator()
    _, rec = run_tick(load_metrics(50), FakeFleet(healthy_fleet(2)), a)

    assert rec.decision == Decision.MAINTAIN
    assert a.ups + a.downs == 0
    assert rec.action == "none"


def test_respects_min_and_max():
    a = FakeActuator()
    _, rec_min = run_tick(load_metrics(5), FakeFleet(healthy_fleet(1)), a)
    assert rec_min.decision == Decision.MAINTAIN

    _, rec_max = run_tick(load_metrics(99), FakeFleet(healthy_fleet(3)), a)
    assert rec_max.decision == Decision.MAINTAIN

    assert a.ups + a.downs == 0


def test_below_min_scales_up_without_metrics():
    a = FakeActuator()
    _, rec = run_tick(FakeMetrics(err=RuntimeError("sin datos")), FakeFleet([]), a)

    assert rec.decision == Decision.INCREASE
    assert a.ups == 1
    assert "observación" in rec.error


def test_observe_error_maintains():
    a = FakeActuator()
    _, rec = run_tick(FakeMetrics(err=RuntimeError("boom")), FakeFleet(healthy_fleet(2)), a)

    assert rec.decision == Decision.MAINTAIN
    assert a.ups + a.downs == 0
    assert "boom" in rec.error


def test_no_metrics_never_scales_down():
    a = FakeActuator()
    _, rec = run_tick(FakeMetrics(), FakeFleet(healthy_fleet(2)), a)

    assert rec.decision == Decision.MAINTAIN
    assert a.downs == 0


def test_fleet_error_does_not_act():
    a = FakeActuator()
    _, rec = run_tick(load_metrics(99), FakeFleet(err=RuntimeError("aws caído")), a)

    assert rec.decision == Decision.MAINTAIN
    assert a.ups == 0
    assert "aws caído" in rec.error


def test_actuator_error_is_recorded():
    a = FakeActuator(err=RuntimeError("InsufficientInstanceCapacity"))
    c, rec = run_tick(load_metrics(99), FakeFleet(healthy_fleet(1)), a)

    assert rec.result == "error"
    assert rec.error != ""
    assert rec.decision == Decision.INCREASE
    assert c.last_action_at is None


def test_cooldown_blocks_consecutive_actions():
    a = FakeActuator()
    l = MemLogger()
    c = Controller(make_config(), load_metrics(99), FakeFleet(healthy_fleet(1)), a, l)

    c.tick()
    c.tick()

    assert a.ups == 1
    assert l.recs[1].reason == "en cooldown"

    c.last_action_at = datetime.now(timezone.utc).astimezone() - timedelta(hours=1)
    c.tick()
    assert a.ups == 2


def test_every_tick_logs_exactly_one_record():
    l = MemLogger()
    c = Controller(
        make_config(),
        FakeMetrics(err=RuntimeError("x")),
        FakeFleet(err=RuntimeError("y")),
        FakeActuator(),
        l,
    )
    c.tick()
    assert len(l.recs) == 1


def test_cycle_id_increments_each_tick():
    l = MemLogger()
    c = Controller(make_config(), load_metrics(50), FakeFleet(healthy_fleet(2)), FakeActuator(), l)
    for _ in range(3):
        c.tick()

    for i, rec in enumerate(l.recs):
        assert rec.cycle_id == i + 1


def test_confirm_cycles_down_requires_consecutive_cycles():
    cfg = make_config()
    cfg.confirm_cycles_down = 3

    a = FakeActuator()
    l = MemLogger()
    c = Controller(cfg, load_metrics(5), FakeFleet(healthy_fleet(2)), a, l)

    # Ciclo 1: carga baja -> MAINTAIN (esperando confirmación 1/3)
    c.tick()
    assert l.recs[0].decision == Decision.MAINTAIN

    # Ciclo 2: carga baja -> MAINTAIN (esperando confirmación 2/3)
    c.tick()
    assert l.recs[1].decision == Decision.MAINTAIN

    # Ciclo 3: carga baja -> REDUCE (confirmación completa 3/3)
    c.tick()
    assert l.recs[2].decision == Decision.REDUCE
    assert a.downs == 1


def test_confirm_cycles_down_resets_on_high_load():
    cfg = make_config()
    cfg.confirm_cycles_down = 3

    l = MemLogger()
    a = FakeActuator()

    c = Controller(cfg, load_metrics(5), FakeFleet(healthy_fleet(2)), a, l)
    c.tick()
    c.tick()

    # Carga alta intermedia que reinicia el contador
    c.metrics = load_metrics(90)
    c.tick()

    # Ahora volvemos a carga baja por 2 ciclos
    c.metrics = load_metrics(5)
    c.tick()
    c.tick()

    assert a.downs == 0


def test_scale_up_step_launches_multiple_instances():
    cfg = make_config()
    cfg.scale_up_step = 2

    a = FakeActuator()
    l = MemLogger()
    c = Controller(cfg, load_metrics(90), FakeFleet(healthy_fleet(1)), a, l)
    c.tick()

    assert a.ups == 2


def test_capacity_before_is_recorded():
    a = FakeActuator()
    _, rec = run_tick(load_metrics(90), FakeFleet(healthy_fleet(2)), a)

    assert rec.capacity_before.total == 2
    assert rec.capacity_before.healthy == 2


def test_last_record_is_updated_after_tick():
    l = MemLogger()
    c = Controller(make_config(), load_metrics(90), FakeFleet(healthy_fleet(1)), FakeActuator(), l)
    c.tick()

    rec = c.last_record
    assert rec is not None
    assert rec.decision == Decision.INCREASE
    assert rec.cycle_id == 1
