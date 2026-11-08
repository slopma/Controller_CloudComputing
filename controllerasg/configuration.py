"""Carga, valores por defecto y validación estricta de la configuración del controlador."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union


@dataclass
class AWSConfig:
    region: str = ""


@dataclass
class ComputeConfig:
    launch_template_id: str = ""
    launch_template_version: str = "$Latest"
    min_instances: int = 1
    max_instances: int = 5
    subnet_ids: list[str] = field(default_factory=list)
    security_group_ids: list[str] = field(default_factory=list)


@dataclass
class LoadBalancerConfig:
    target_group_arn: str = ""
    target_port: int = 80


@dataclass
class ScalingConfig:
    scale_up_threshold: float = 0.0
    scale_down_threshold: float = 0.0


@dataclass
class Dimension:
    name: str = ""
    value: str = ""


@dataclass
class MetricPolicy:
    namespace: str = ""
    metric_name: str = ""
    dimensions: list[Dimension] = field(default_factory=list)
    per_instance: bool = False
    period: int = 60
    stat: str = "Average"
    scaling: ScalingConfig = field(default_factory=ScalingConfig)


@dataclass
class DeploymentConfig:
    use_docker: bool = False


@dataclass
class Config:
    aws: AWSConfig = field(default_factory=AWSConfig)
    compute: ComputeConfig = field(default_factory=ComputeConfig)
    load_balancer: LoadBalancerConfig = field(default_factory=LoadBalancerConfig)

    poll_interval_seconds: int = 60
    observation_window_seconds: int = 300
    cooldown_seconds: int = 300

    scale_up_step: int = 1
    scale_down_step: int = 1
    confirm_cycles_down: int = 1

    status_port: int = 0
    state_file: str = ""

    metrics: list[MetricPolicy] = field(default_factory=list)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)

    def defaults(self) -> None:
        """Aplica valores por defecto a campos numéricos opcionales."""
        if self.scale_up_step <= 0:
            self.scale_up_step = 1
        if self.scale_down_step <= 0:
            self.scale_down_step = 1
        if self.confirm_cycles_down <= 0:
            self.confirm_cycles_down = 1

    def validate(self) -> None:
        """Valida exhaustivamente la consistencia de los parámetros del controlador."""
        if not self.aws.region:
            raise ValueError("aws.region es obligatorio")
        if not self.compute.launch_template_id:
            raise ValueError("compute.launchTemplateId es obligatorio")
        if not self.load_balancer.target_group_arn:
            raise ValueError("loadBalancer.targetGroupArn es obligatorio")
        if not self.compute.subnet_ids:
            raise ValueError("compute.subnetIds necesita al menos una subred")
        if self.compute.min_instances < 1:
            raise ValueError("minInstances debe ser al menos 1")
        if self.compute.max_instances > 5:
            raise ValueError("maxInstances debe ser como máximo 5")
        if self.compute.min_instances > self.compute.max_instances:
            raise ValueError("minInstances debe ser menor o igual a maxInstances")
        if self.poll_interval_seconds <= 0:
            raise ValueError("pollIntervalSeconds debe ser mayor que 0")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldownSeconds no puede ser negativo")
        if self.scale_up_step not in (1, 2):
            raise ValueError("scaleUpStep debe ser 1 o 2")
        if self.scale_down_step not in (1, 2):
            raise ValueError("scaleDownStep debe ser 1 o 2")
        if self.confirm_cycles_down < 1:
            raise ValueError("confirmCyclesDown debe ser al menos 1")
        if self.status_port < 0 or self.status_port > 65535:
            raise ValueError("statusPort debe estar entre 0 y 65535")
        if not self.metrics:
            raise ValueError("se necesita al menos 1 política de métrica")

        for m in self.metrics:
            if not m.namespace or not m.metric_name or not m.stat:
                raise ValueError(
                    f'métrica "{m.metric_name}": namespace, metricName y stat son obligatorios'
                )
            if m.per_instance and len(m.dimensions) > 0:
                raise ValueError(
                    f'métrica "{m.metric_name}": perInstance no debe traer dimensions, se generan una por instancia'
                )
            if m.period <= 0 or m.period % 60 != 0:
                raise ValueError(
                    f'métrica "{m.metric_name}": period debe ser un múltiplo positivo de 60'
                )
            if self.observation_window_seconds < m.period:
                raise ValueError(
                    f'observationWindowSeconds debe ser al menos el period de "{m.metric_name}"'
                )
            if m.scaling.scale_up_threshold <= m.scaling.scale_down_threshold:
                raise ValueError(
                    f'métrica "{m.metric_name}": scaleUpThreshold debe ser mayor que scaleDownThreshold'
                )


def _get(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in data:
            return data[k]
    return default


def parse_config(data: dict[str, Any]) -> Config:
    """Construye un objeto Config a partir de un diccionario JSON."""
    aws_data = data.get("aws", {})
    aws_cfg = AWSConfig(
        region=_get(aws_data, "region", default="")
    )

    compute_data = data.get("compute", {})
    compute_cfg = ComputeConfig(
        launch_template_id=_get(compute_data, "launchTemplateId", "launch_template_id", default=""),
        launch_template_version=_get(compute_data, "launchTemplateVersion", "launch_template_version", default="$Latest"),
        min_instances=int(_get(compute_data, "minInstances", "min_instances", default=1)),
        max_instances=int(_get(compute_data, "maxInstances", "max_instances", default=5)),
        subnet_ids=list(_get(compute_data, "subnetIds", "subnet_ids", default=[])),
        security_group_ids=list(_get(compute_data, "securityGroupIds", "security_group_ids", default=[])),
    )

    lb_data = _get(data, "loadBalancer", "load_balancer", default={})
    lb_cfg = LoadBalancerConfig(
        target_group_arn=_get(lb_data, "targetGroupArn", "target_group_arn", default=""),
        target_port=int(_get(lb_data, "targetPort", "target_port", default=80)),
    )

    metrics_list: list[MetricPolicy] = []
    for m in data.get("metrics", []):
        dims: list[Dimension] = []
        for d in m.get("dimensions", []):
            dims.append(Dimension(name=d.get("name", ""), value=str(d.get("value", ""))))

        sc_data = m.get("scaling", {})
        sc_cfg = ScalingConfig(
            scale_up_threshold=float(_get(sc_data, "scaleUpThreshold", "scale_up_threshold", default=0.0)),
            scale_down_threshold=float(_get(sc_data, "scaleDownThreshold", "scale_down_threshold", default=0.0)),
        )

        metrics_list.append(MetricPolicy(
            namespace=_get(m, "namespace", default=""),
            metric_name=_get(m, "metricName", "metric_name", default=""),
            dimensions=dims,
            per_instance=bool(_get(m, "perInstance", "per_instance", default=False)),
            period=int(_get(m, "period", default=60)),
            stat=_get(m, "stat", default="Average"),
            scaling=sc_cfg,
        ))

    dep_data = data.get("deployment", {})
    dep_cfg = DeploymentConfig(
        use_docker=bool(_get(dep_data, "useDocker", "use_docker", default=False))
    )

    cfg = Config(
        aws=aws_cfg,
        compute=compute_cfg,
        load_balancer=lb_cfg,
        poll_interval_seconds=int(_get(data, "pollIntervalSeconds", "poll_interval_seconds", default=60)),
        observation_window_seconds=int(_get(data, "observationWindowSeconds", "observation_window_seconds", default=300)),
        cooldown_seconds=int(_get(data, "cooldownSeconds", "cooldown_seconds", default=300)),
        scale_up_step=int(_get(data, "scaleUpStep", "scale_up_step", default=1)),
        scale_down_step=int(_get(data, "scaleDownStep", "scale_down_step", default=1)),
        confirm_cycles_down=int(_get(data, "confirmCyclesDown", "confirm_cycles_down", default=1)),
        status_port=int(_get(data, "statusPort", "status_port", default=0)),
        state_file=str(_get(data, "stateFile", "state_file", default="")),
        metrics=metrics_list,
        deployment=dep_cfg,
    )

    cfg.defaults()
    cfg.validate()
    return cfg


def load_config(path: Union[str, Path]) -> Config:
    """Carga y valida un archivo de configuración en formato JSON."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise ValueError(f"leyendo config: {e}") from e

    return parse_config(data)
