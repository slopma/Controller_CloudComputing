# consulta de metricas en cloudwatch

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from controllerasg.configuration import MetricPolicy
from controllerasg.model import Instance, ObservedMetric


# estructura simple para guardar los datapoints
@dataclass
class Series:
    values: list[float] = field(default_factory=list)
    newest: Optional[datetime] = None


class CloudWatchObserver:
    def __init__(
        self,
        client: Any,
        metrics: Sequence[MetricPolicy],
        window: timedelta,
    ) -> None:
        self.client = client
        self.metrics = list(metrics)
        self.window = window

    # consulta todas las metricas y devuelve la lista con los promedios
    def observe(self, fleet: Sequence[Instance]) -> list[ObservedMetric]:
        queries, ids = self.build_queries(fleet)
        if not queries:
            return []

        raw = self.fetch_series(queries)
        return self.aggregate(raw, ids)

    # arma los diccionarios de consulta para GetMetricData
    def build_queries(
        self, fleet: Sequence[Instance]
    ) -> tuple[list[dict[str, Any]], dict[int, list[str]]]:
        queries: list[dict[str, Any]] = []
        ids: dict[int, list[str]] = {i: [] for i in range(len(self.metrics))}

        for i, m in enumerate(self.metrics):
            if m.per_instance:
                # si es por instancia, se consulta una por una
                for inst in fleet:
                    qid = f"m{i}_{inst.id.replace('-', '')}"
                    ids[i].append(qid)
                    queries.append(
                        self._metric_query(
                            qid,
                            m.namespace,
                            m.metric_name,
                            m.period,
                            m.stat,
                            [{"Name": "InstanceId", "Value": inst.id}],
                        )
                    )
                continue

            # metrica normal con dimensiones fijas
            qid = f"m{i}"
            ids[i].append(qid)
            dimensions = [{"Name": d.name, "Value": d.value} for d in m.dimensions]
            queries.append(
                self._metric_query(
                    qid,
                    m.namespace,
                    m.metric_name,
                    m.period,
                    m.stat,
                    dimensions,
                )
            )

        return queries, ids

    @staticmethod
    def _metric_query(
        qid: str,
        namespace: str,
        metric_name: str,
        period: int,
        stat: str,
        dimensions: list[dict[str, str]],
    ) -> dict[str, Any]:
        return {
            "Id": qid,
            "MetricStat": {
                "Metric": {
                    "Namespace": namespace,
                    "MetricName": metric_name,
                    "Dimensions": dimensions,
                },
                "Period": period,
                "Stat": stat,
            },
        }

    # hace la llamada a aws y maneja la paginacion si hay muchos datos
    def fetch_series(self, queries: list[dict[str, Any]]) -> dict[str, Series]:
        now = datetime.now(timezone.utc)
        start_time = now - self.window
        raw: dict[str, Series] = {}

        next_token: Optional[str] = None
        while True:
            params: dict[str, Any] = {
                "MetricDataQueries": queries,
                "StartTime": start_time,
                "EndTime": now,
                "ScanBy": "TimestampDescending",
            }
            if next_token:
                params["NextToken"] = next_token

            out = self.client.get_metric_data(**params)

            for r in out.get("MetricDataResults", []):
                qid = r.get("Id", "")
                status_code = r.get("StatusCode")
                if status_code == "Forbidden":
                    raise RuntimeError(f"GetMetricData: acceso denegado a la consulta {qid}")

                if qid not in raw:
                    raw[qid] = Series()

                values = r.get("Values", [])
                raw[qid].values.extend(values)

                for ts in r.get("Timestamps", []):
                    if raw[qid].newest is None or ts > raw[qid].newest:
                        raw[qid].newest = ts

            next_token = out.get("NextToken")
            if not next_token:
                break

        return raw

    # saca el promedio de cada metrica en la ventana
    def aggregate(
        self, raw: dict[str, Series], ids: dict[int, list[str]]
    ) -> list[ObservedMetric]:
        result: list[ObservedMetric] = []

        for i, m in enumerate(self.metrics):
            sum_val = 0.0
            n = 0
            newest: Optional[datetime] = None

            for qid in ids.get(i, []):
                s = raw.get(qid)
                if not s or not s.values:
                    continue

                avg = sum(s.values) / float(len(s.values))
                sum_val += avg
                n += 1

                if s.newest is not None:
                    if newest is None or s.newest > newest:
                        newest = s.newest

            # si no hay datos, se omite para no inventar ceros
            if n == 0:
                continue

            ts = newest if newest is not None else datetime.now(timezone.utc)
            result.append(
                ObservedMetric(
                    name=m.metric_name,
                    value=sum_val / float(n),
                    timestamp=ts,
                )
            )

        return result
