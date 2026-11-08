"""Bucle cerrado de control: observar -> decidir -> actuar."""

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from controllerasg.configuration import Config
from controllerasg.model import (
    Capacity,
    Decision,
    DecisionRecord,
    Instance,
    InstanceStatus,
    ObservedMetric,
)

logger = logging.getLogger(__name__)


def summarize(fleet: Sequence[Instance]) -> Capacity:
    """Genera el resumen de capacidad y salud de la flota actual."""
    total = len(fleet)
    healthy = sum(1 for i in fleet if i.health == "healthy")
    instances = [
        InstanceStatus(id=i.id, state=i.state, health=i.health)
        for i in fleet
    ]
    return Capacity(total=total, healthy=healthy, instances=instances)


class Controller:
    """Orquestador del bucle cerrado de auto-scaling con histéresis temporal y persistencia."""

    def __init__(
        self,
        config: Config,
        metrics_source: Any,
        fleet_source: Any,
        actuator: Any,
        logger_instance: Any,
    ) -> None:
        self.config = config
        self.metrics = metrics_source
        self.fleet = fleet_source
        self.actuator = actuator
        self.logger = logger_instance

        self.last_action_at: Optional[datetime] = None
        self.cycle_id: int = 0
        self.confirm_count: int = 0

        self._lock = threading.Lock()
        self._last_record: Optional[DecisionRecord] = None
        self._last_record_lock = threading.Lock()

        self.load_state()

    @property
    def last_record(self) -> Optional[DecisionRecord]:
        """Devuelve el último registro generado (para inspección vía /status HTTP)."""
        with self._last_record_lock:
            return self._last_record

    def tick(self) -> None:
        """Ejecuta un ciclo completo: observar -> decidir -> actuar y registra exactamente un DecisionRecord."""
        with self._lock:
            self.cycle_id += 1
            cycle_id = self.cycle_id

        rec = DecisionRecord(
            cycle_id=cycle_id,
            time=datetime.now(timezone.utc).astimezone(),
            decision=Decision.MAINTAIN,
            reason="",
            window_seconds=self.config.observation_window_seconds,
            metrics=[],
            capacity_before=Capacity(),
            capacity=Capacity(),
            action="none",
            result="none",
            error="",
        )

        try:
            # --- 1. Observar flota ---
            try:
                fleet = self.fleet.instances()
            except Exception as e:
                rec.reason = "no se pudo leer la flota, no se actúa"
                rec.error = str(e)
                return

            cap = summarize(fleet)
            rec.capacity = cap
            rec.capacity_before = cap

            # --- 2. Observar métricas ---
            observed: list[ObservedMetric] = []
            obs_err: Optional[Exception] = None
            try:
                observed = self.metrics.observe(fleet)
            except Exception as e:
                obs_err = e
                rec.error = f"observación: {e}"
            rec.metrics = observed

            # --- 3. Decidir ---
            with self._lock:
                decision, reason, confirm_cycle = self.decide(observed, len(fleet))
                rec.confirm_cycle = confirm_cycle

            rec.decision = decision
            rec.reason = reason
            if obs_err is not None and rec.decision == Decision.MAINTAIN:
                rec.reason = "métricas no disponibles, no se actúa"

            # --- 4. Actuar ---
            action_err: Optional[Exception] = None
            if rec.decision == Decision.INCREASE:
                rec.action = "ScaleUp"
                for _ in range(self.config.scale_up_step):
                    try:
                        inst_id = self.actuator.scale_up()
                        rec.instance_id = inst_id
                    except Exception as e:
                        action_err = e
                        break

            elif rec.decision == Decision.REDUCE:
                rec.action = "ScaleDown"
                for _ in range(self.config.scale_down_step):
                    try:
                        inst_id = self.actuator.scale_down()
                        rec.instance_id = inst_id
                    except Exception as e:
                        action_err = e
                        break
            else:
                return

            if action_err is not None:
                rec.result = "error"
                rec.error = str(action_err)
                return

            rec.result = "ok"

            with self._lock:
                self.last_action_at = datetime.now(timezone.utc).astimezone()
            self.save_state()

            # Leer la flota posterior a la acción para reflejarla en la auditoría
            try:
                after_fleet = self.fleet.instances()
                rec.capacity_after = summarize(after_fleet)
            except Exception as e:
                logger.warning(f"[controller] aviso: no se pudo leer la flota post-acción: {e}")

        finally:
            self.logger.log(rec)
            with self._last_record_lock:
                self._last_record = rec

    def decide(
        self, observed: Sequence[ObservedMetric], current_count: int
    ) -> tuple[Decision, str, int]:
        """Implementa la política asimétrica de escalado con histéresis temporal."""
        # 1. Regla de seguridad prioritaria: restaurar el mínimo sin importar métricas ni cooldown
        if current_count < self.config.compute.min_instances:
            self.confirm_count = 0
            return Decision.INCREASE, "capacidad por debajo del mínimo configurado", 0

        # 2. Cooldown: no actuar si la última acción fue reciente
        if self.last_action_at is not None:
            now = datetime.now(timezone.utc).astimezone()
            elapsed = (now - self.last_action_at).total_seconds()
            if elapsed < self.config.cooldown_seconds:
                return Decision.MAINTAIN, "en cooldown", self.confirm_count

        # 3. Sin métricas disponibles: mantener siempre
        if not observed:
            self.confirm_count = 0
            return Decision.MAINTAIN, "sin datos de métricas, no se actúa", 0

        values = {m.name: m.value for m in observed}

        any_above_up = False
        all_below_down = True

        for policy in self.config.metrics:
            if policy.metric_name not in values:
                # Una métrica sin datos se abstiene: no fuerza el escalado hacia arriba ni bloquea la reducción
                continue
            v = values[policy.metric_name]
            if v >= policy.scaling.scale_up_threshold:
                any_above_up = True
            if v > policy.scaling.scale_down_threshold:
                all_below_down = False

        if any_above_up and current_count < self.config.compute.max_instances:
            self.confirm_count = 0
            return Decision.INCREASE, "al menos una métrica superó su umbral de subida", 0

        if all_below_down and current_count > self.config.compute.min_instances:
            self.confirm_count += 1
            if self.confirm_count >= self.config.confirm_cycles_down:
                self.confirm_count = 0
                return (
                    Decision.REDUCE,
                    "todas las métricas bajo umbral durante los ciclos de confirmación requeridos",
                    self.config.confirm_cycles_down,
                )
            return (
                Decision.MAINTAIN,
                "métricas bajo umbral, esperando confirmación de ciclos antes de reducir",
                self.confirm_count,
            )

        self.confirm_count = 0
        return (
            Decision.MAINTAIN,
            "condiciones estables, métricas incompletas o en límites de min/max",
            0,
        )

    def load_state(self) -> None:
        """Restaura el instante de la última acción desde el archivo de estado en disco si existe."""
        if not self.config.state_file:
            return
        path = Path(self.config.state_file)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            last_action_str = data.get("lastActionAt")
            if last_action_str:
                self.last_action_at = datetime.fromisoformat(last_action_str)
                logger.info(
                    f"[controller] cooldown restaurado: última acción en {self.last_action_at.isoformat()}"
                )
        except Exception as e:
            logger.warning(
                f"[controller] aviso: no se pudo leer el estado persistido ({path}): {e}"
            )

    def save_state(self) -> None:
        """Persiste el instante de la última acción al archivo de estado."""
        if not self.config.state_file or self.last_action_at is None:
            return
        path = Path(self.config.state_file)
        try:
            data = {"lastActionAt": self.last_action_at.isoformat()}
            path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(
                f"[controller] error al guardar estado ({path}): {e}"
            )
