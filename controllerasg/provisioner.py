"""Aprovisionador de infraestructura: gestión del ciclo de vida de instancias EC2 y Target Group."""

import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from controllerasg.configuration import Config
from controllerasg.model import Instance

MANAGED_TAG_KEY = "managed-by"
MANAGED_TAG_VALUE = "controllerasg"

HEALTH_UNREGISTERED = "unregistered"
HEALTH_HEALTHY = "healthy"
HEALTH_INITIAL = "initial"
HEALTH_DRAINING = "draining"
HEALTH_UNUSED = "unused"
HEALTH_UNHEALTHY = "unhealthy"


def pick_subnet(subnets: Sequence[str], fleet: Sequence[Instance]) -> str:
    """Distribuye las instancias entre zonas de disponibilidad seleccionando la subred menos poblada."""
    if not subnets:
        raise ValueError("lista de subredes vacía")
    if not fleet:
        return subnets[0]

    count: dict[str, int] = {s: 0 for s in subnets}
    for inst in fleet:
        if inst.subnet_id in count:
            count[inst.subnet_id] += 1

    best = subnets[0]
    for s in subnets[1:]:
        if count[s] < count[best]:
            best = s
    return best


def pick_victim(fleet: Sequence[Instance]) -> Instance:
    """Selecciona la instancia a retirar: primero las no saludables, y ante empate la más reciente."""
    if not fleet:
        raise ValueError("flota vacía")

    def sort_key(inst: Instance) -> tuple[int, float]:
        is_healthy = 1 if inst.health == HEALTH_HEALTHY else 0
        ts = 0.0
        if inst.launch_time is not None:
            ts = inst.launch_time.timestamp()
        return (is_healthy, -ts)

    sorted_fleet = sorted(fleet, key=sort_key)
    return sorted_fleet[0]


def warming_up(fleet: Sequence[Instance]) -> str:
    """Identifica si existe alguna instancia en proceso de arranque o inicialización."""
    for inst in fleet:
        if inst.state == "pending" or inst.health == HEALTH_INITIAL:
            return inst.id
    return ""


class Provisioner:
    """Controlador de recursos EC2 y balanceador ELBv2."""

    def __init__(
        self,
        ec2_client: Any,
        elb_client: Any,
        config: Config,
        running_timeout: float = 180.0,
        drain_timeout: float = 120.0,
        drain_poll: float = 5.0,
    ) -> None:
        self.ec2 = ec2_client
        self.elb = elb_client
        self.config = config
        self.running_timeout = running_timeout
        self.drain_timeout = drain_timeout
        self.drain_poll = drain_poll

    def instances(self) -> list[Instance]:
        """Consulta las instancias gestionadas (pending o running) y su estado de salud en el Target Group."""
        fleet: list[Instance] = []

        params: dict[str, Any] = {
            "Filters": [
                {"Name": f"tag:{MANAGED_TAG_KEY}", "Values": [MANAGED_TAG_VALUE]},
                {"Name": "instance-state-name", "Values": ["pending", "running"]},
            ]
        }

        while True:
            out = self.ec2.describe_instances(**params)
            for r in out.get("Reservations", []):
                for i in r.get("Instances", []):
                    inst_id = i.get("InstanceId", "")
                    subnet_id = i.get("SubnetId", "")
                    state = i.get("State", {}).get("Name", "")
                    launch_time = i.get("LaunchTime")
                    ip = i.get("PrivateIpAddress", "")

                    fleet.append(
                        Instance(
                            id=inst_id,
                            subnet_id=subnet_id,
                            state=state,
                            health=HEALTH_UNREGISTERED,
                            launch_time=launch_time,
                            ip=ip,
                        )
                    )

            next_token = out.get("NextToken")
            if not next_token:
                break
            params["NextToken"] = next_token

        health_map = self.target_health()
        for inst in fleet:
            if inst.id in health_map:
                inst.health = health_map[inst.id]

        return fleet

    def target_health(self) -> dict[str, str]:
        """Obtiene el mapa de salud de los targets registrados en el Target Group."""
        health: dict[str, str] = {}
        try:
            out = self.elb.describe_target_health(
                TargetGroupArn=self.config.load_balancer.target_group_arn
            )
            for d in out.get("TargetHealthDescriptions", []):
                target = d.get("Target", {})
                target_health_info = d.get("TargetHealth", {})
                tid = target.get("Id")
                state = target_health_info.get("State")
                if tid and state:
                    health[tid] = state
        except Exception:
            raise
        return health

    def scale_up(self) -> str:
        """Lanza una nueva instancia desde el Launch Template y la registra en el Target Group.

        Aplica rollback automático si la instancia no alcanza running o falla el registro.
        """
        fleet = self.instances()
        if len(fleet) >= self.config.compute.max_instances:
            raise RuntimeError(
                f"ya hay {len(fleet)} instancias, el máximo es {self.config.compute.max_instances}"
            )

        subnet = pick_subnet(self.config.compute.subnet_ids, fleet)

        run_params: dict[str, Any] = {
            "SubnetId": subnet,
            "LaunchTemplate": {
                "LaunchTemplateId": self.config.compute.launch_template_id,
                "Version": self.config.compute.launch_template_version,
            },
            "MinCount": 1,
            "MaxCount": 1,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": MANAGED_TAG_KEY, "Value": MANAGED_TAG_VALUE}
                    ],
                }
            ],
        }
        if self.config.compute.security_group_ids:
            run_params["SecurityGroupIds"] = self.config.compute.security_group_ids

        run = self.ec2.run_instances(**run_params)
        instances = run.get("Instances", [])
        if not instances:
            raise RuntimeError("RunInstances no devolvió instancias")

        inst_id = instances[0].get("InstanceId", "")

        self._wait_running(inst_id)

        try:
            self.elb.register_targets(
                TargetGroupArn=self.config.load_balancer.target_group_arn,
                Targets=[
                    {
                        "Id": inst_id,
                        "Port": self.config.load_balancer.target_port,
                    }
                ],
            )
        except Exception as e:
            self.rollback(inst_id)
            raise RuntimeError(f"RegisterTargets de {inst_id}: {e}") from e

        return inst_id

    def _wait_running(self, inst_id: str) -> None:
        """Espera a que la instancia se encuentre en estado running."""
        if hasattr(self.ec2, "get_waiter"):
            try:
                waiter = self.ec2.get_waiter("instance_running")
                waiter.wait(
                    InstanceIds=[inst_id],
                    WaiterConfig={
                        "Delay": 1,
                        "MaxAttempts": max(1, int(self.running_timeout)),
                    },
                )
                return
            except Exception as e:
                # Si falla o no está soportado en mocks, intentamos polling directo
                if "la instancia" in str(e):
                    raise
                pass

        deadline = time.time() + self.running_timeout
        while time.time() < deadline:
            try:
                res = self.ec2.describe_instances(InstanceIds=[inst_id])
                for r in res.get("Reservations", []):
                    for i in r.get("Instances", []):
                        if i.get("State", {}).get("Name") == "running":
                            return
            except Exception:
                pass
            time.sleep(min(0.2, self.running_timeout / 5))

        self.rollback(inst_id)
        raise RuntimeError(f"la instancia {inst_id} no llegó a running: tiempo de espera agotado")

    def scale_down(self) -> str:
        """Selecciona una instancia víctima, drena sus conexiones y la termina."""
        fleet = self.instances()
        if len(fleet) <= self.config.compute.min_instances:
            raise RuntimeError(
                f"hay {len(fleet)} instancias, el mínimo es {self.config.compute.min_instances}"
            )

        warming = warming_up(fleet)
        if warming:
            raise RuntimeError(
                f"no es seguro reducir: {warming} aún se está inicializando"
            )

        victim = pick_victim(fleet)

        if victim.health != HEALTH_UNREGISTERED:
            try:
                self.elb.deregister_targets(
                    TargetGroupArn=self.config.load_balancer.target_group_arn,
                    Targets=[
                        {
                            "Id": victim.id,
                            "Port": self.config.load_balancer.target_port,
                        }
                    ],
                )
            except Exception as e:
                raise RuntimeError(f"DeregisterTargets de {victim.id}: {e}") from e

            self.wait_drained(victim.id)

        try:
            self.ec2.terminate_instances(InstanceIds=[victim.id])
        except Exception as e:
            raise RuntimeError(f"TerminateInstances de {victim.id}: {e}") from e

        return victim.id

    def wait_drained(self, inst_id: str) -> None:
        """Espera a que las conexiones activas terminen de drenarse en el Target Group."""
        deadline = time.time() + self.drain_timeout
        while time.time() < deadline:
            try:
                health = self.target_health()
                if health.get(inst_id) != HEALTH_DRAINING:
                    return
            except Exception:
                return
            time.sleep(self.drain_poll)

    def rollback(self, inst_id: str) -> None:
        """Termina una instancia que no pudo completar su registro operativo."""
        try:
            self.ec2.terminate_instances(InstanceIds=[inst_id])
        except Exception:
            pass
