# modelos de datos para el controller

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


# decisiones que puede tomar el controller
class Decision(str, Enum):
    MAINTAIN = "MAINTAIN_CAPACITY"
    INCREASE = "INCREASE_CAPACITY"
    REDUCE = "REDUCE_CAPACITY"


# estado basico de una instancia
@dataclass
class InstanceStatus:
    id: str
    state: str
    health: str

    def to_dict(self) -> dict[str, str]:
        return {
            "ID": self.id,
            "State": self.state,
            "Health": self.health,
        }


# cuantas instancias hay y cuantas saludables
@dataclass
class Capacity:
    total: int = 0
    healthy: int = 0
    instances: list[InstanceStatus] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "Total": self.total,
            "Healthy": self.healthy,
            "Instances": [inst.to_dict() for inst in self.instances],
        }


# metrica que devolvio cloudwatch
@dataclass
class ObservedMetric:
    name: str
    value: float
    timestamp: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "Name": self.name,
            "Value": self.value,
            "timestamp": self.timestamp.isoformat(),
        }


# info de una instancia ec2 en aws
@dataclass
class Instance:
    id: str
    ip: str = ""
    subnet_id: str = ""
    state: str = ""  # pending, running, etc
    health: str = "unregistered"  # healthy, initial, etc
    launch_time: Optional[datetime] = None
    actual_metrics: list[ObservedMetric] = field(default_factory=list)


@dataclass
class GroupInstances:
    instances: list[Instance] = field(default_factory=list)


# lo que se escribe en el jsonl cada ciclo
@dataclass
class DecisionRecord:
    cycle_id: int
    time: datetime
    decision: Decision
    reason: str
    window_seconds: int
    metrics: list[ObservedMetric]
    capacity_before: Capacity
    capacity: Capacity
    capacity_after: Optional[Capacity] = None
    confirm_cycle: int = 0
    instance_id: str = ""
    action: str = "none"
    result: str = "none"
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "CycleID": self.cycle_id,
            "Time": self.time.isoformat(),
            "Decision": self.decision.value if isinstance(self.decision, Decision) else str(self.decision),
            "Reason": self.reason,
            "WindowSeconds": self.window_seconds,
            "Metrics": [m.to_dict() for m in self.metrics],
            "CapacityBefore": self.capacity_before.to_dict() if self.capacity_before else None,
            "Capacity": self.capacity.to_dict() if self.capacity else None,
            "InstanceID": self.instance_id,
            "Action": self.action,
            "Result": self.result,
            "Error": self.error,
        }
        if self.capacity_after is not None:
            d["CapacityAfter"] = self.capacity_after.to_dict()
        if self.confirm_cycle > 0:
            d["ConfirmCycle"] = self.confirm_cycle
        return d
