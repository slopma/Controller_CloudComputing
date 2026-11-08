"""Pruebas unitarias para el módulo de configuración."""

import json
import pytest
from pathlib import Path

from controllerasg.configuration import load_config, parse_config, Config


def valid_json_dict() -> dict:
    return {
        "aws": {"region": "us-east-1"},
        "compute": {
            "launchTemplateId": "lt-12345",
            "minInstances": 1,
            "maxInstances": 5,
            "subnetIds": ["subnet-1"],
        },
        "loadBalancer": {
            "targetGroupArn": "arn:aws:tg-1",
            "targetPort": 80,
        },
        "pollIntervalSeconds": 60,
        "observationWindowSeconds": 300,
        "cooldownSeconds": 300,
        "metrics": [
            {
                "namespace": "AWS/EC2",
                "metricName": "CPUUtilization",
                "stat": "Average",
                "period": 60,
                "scaling": {"scaleUpThreshold": 70, "scaleDownThreshold": 20},
            }
        ],
    }


def test_load_defaults(tmp_path: Path):
    path = tmp_path / "conf.json"
    path.write_text(json.dumps(valid_json_dict()), encoding="utf-8")

    cfg = load_config(path)

    assert cfg.scale_up_step == 1
    assert cfg.scale_down_step == 1
    assert cfg.confirm_cycles_down == 1
    assert cfg.status_port == 0


def test_load_custom_fields(tmp_path: Path):
    custom = valid_json_dict()
    custom["scaleUpStep"] = 2
    custom["scaleDownStep"] = 1
    custom["confirmCyclesDown"] = 3
    custom["statusPort"] = 9090
    custom["stateFile"] = "state.json"

    path = tmp_path / "conf.json"
    path.write_text(json.dumps(custom), encoding="utf-8")

    cfg = load_config(path)

    assert cfg.scale_up_step == 2
    assert cfg.confirm_cycles_down == 3
    assert cfg.status_port == 9090
    assert cfg.state_file == "state.json"


@pytest.mark.parametrize(
    "mutator,expected_msg",
    [
        (lambda c: setattr(c.compute, "min_instances", 5) or setattr(c.compute, "max_instances", 2), "menor o igual"),
        (lambda c: setattr(c, "scale_up_step", 0), "scaleUpStep debe ser 1 o 2"),
        (lambda c: setattr(c, "confirm_cycles_down", 0), "confirmCyclesDown debe ser al menos 1"),
        (lambda c: setattr(c, "status_port", 70000), "statusPort debe estar entre 0 y 65535"),
        (lambda c: setattr(c.aws, "region", ""), "aws.region es obligatorio"),
        (lambda c: setattr(c.compute, "launch_template_id", ""), "launchTemplateId es obligatorio"),
        (lambda c: setattr(c.load_balancer, "target_group_arn", ""), "targetGroupArn es obligatorio"),
        (lambda c: setattr(c.compute, "subnet_ids", []), "necesita al menos una subred"),
        (lambda c: setattr(c, "metrics", []), "al menos 1 política"),
    ],
)
def test_validation_rejects_invalid(mutator, expected_msg):
    cfg = parse_config(valid_json_dict())
    mutator(cfg)
    with pytest.raises(ValueError) as excinfo:
        cfg.validate()
    assert expected_msg in str(excinfo.value)
