#!/usr/bin/env python3
"""
analysis/analyze.py — Analiza el log de decisiones del ControllerASG.

Genera:
  - Resumen estadístico por consola
  - decisions_analysis.png: gráfica de capacidad, métricas y decisiones en el tiempo

Uso:
  python3 analysis/analyze.py decisions.jsonl
  python3 analysis/analyze.py decisions.jsonl --no-plot   # solo texto
"""

import json
import sys
import argparse
from datetime import datetime, timezone
from collections import Counter


def load_records(path: str) -> list[dict]:
    records = []
    with open(path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[aviso] línea {i} malformada: {e}", file=sys.stderr)
    return records


def parse_time(s: str) -> datetime:
    # Maneja offsets como -05:00 (Python < 3.11 no admite 'Z' con %z directamente)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))


def print_summary(records: list[dict]) -> None:
    if not records:
        print("No hay registros.")
        return

    decisions = Counter(r.get("Decision", "?") for r in records)
    errors = [r for r in records if r.get("Error")]
    scale_ups = [r for r in records if r.get("Action") == "ScaleUp" and r.get("Result") == "ok"]
    scale_downs = [r for r in records if r.get("Action") == "ScaleDown" and r.get("Result") == "ok"]

    # Calcular tiempo desde primera hasta última decisión
    times = [parse_time(r["Time"]) for r in records if "Time" in r]
    duration_min = (max(times) - min(times)).total_seconds() / 60 if len(times) > 1 else 0

    # Capacidad a lo largo del tiempo
    capacities = [r.get("CapacityBefore", r.get("Capacity", {})).get("Total", "?") for r in records]
    cap_nums = [c for c in capacities if isinstance(c, int)]

    print("=" * 60)
    print("  ANÁLISIS DEL LOG DE DECISIONES — ControllerASG")
    print("=" * 60)
    print(f"  Registros totales   : {len(records)}")
    print(f"  Duración observada  : {duration_min:.1f} min")
    print(f"  Primer ciclo        : {min(times).strftime('%Y-%m-%d %H:%M:%S %Z') if times else 'N/A'}")
    print(f"  Último ciclo        : {max(times).strftime('%Y-%m-%d %H:%M:%S %Z') if times else 'N/A'}")
    print()
    print("  Decisiones:")
    for d, count in sorted(decisions.items()):
        pct = count / len(records) * 100
        print(f"    {d:<25} {count:>4} ({pct:.1f}%)")
    print()
    print(f"  ScaleUp  exitosos   : {len(scale_ups)}")
    print(f"  ScaleDown exitosos  : {len(scale_downs)}")
    print(f"  Ciclos con error    : {len(errors)}")
    if cap_nums:
        print(f"  Capacidad mín/máx   : {min(cap_nums)} / {max(cap_nums)} instancias")
        avg_cap = sum(cap_nums) / len(cap_nums)
        print(f"  Capacidad promedio  : {avg_cap:.2f} instancias")
    print()

    # Motivos de decisión más frecuentes
    reasons = Counter(r.get("Reason", "?") for r in records)
    print("  Motivos más frecuentes:")
    for reason, count in reasons.most_common(5):
        print(f"    [{count:>3}] {reason}")
    print()

    # Métricas observadas
    metric_names: set[str] = set()
    for r in records:
        for m in r.get("Metrics", []):
            metric_names.add(m.get("Name", "?"))
    print(f"  Métricas en el log  : {', '.join(sorted(metric_names)) or 'ninguna'}")
    print("=" * 60)


def plot(records: list[dict], output: str = "decisions_analysis.png") -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        import matplotlib.patches as mpatches
    except ImportError:
        print("[aviso] matplotlib no instalado. Ejecuta: pip install matplotlib", file=sys.stderr)
        return

    times = [parse_time(r["Time"]) for r in records if "Time" in r]
    if not times:
        print("[aviso] No hay timestamps válidos para graficar.")
        return

    capacities = [r.get("CapacityBefore", r.get("Capacity", {})).get("Total", None) for r in records]
    decisions = [r.get("Decision", "") for r in records]

    # Color por decisión
    color_map = {
        "MAINTAIN_CAPACITY": "#4CAF50",
        "INCREASE_CAPACITY": "#F44336",
        "REDUCE_CAPACITY":   "#2196F3",
    }
    colors = [color_map.get(d, "#9E9E9E") for d in decisions]

    # Métricas disponibles
    all_metric_names: list[str] = []
    for r in records:
        for m in r.get("Metrics", []):
            n = m.get("Name", "")
            if n and n not in all_metric_names:
                all_metric_names.append(n)

    n_subplots = 1 + (1 if all_metric_names else 0)
    fig, axes = plt.subplots(n_subplots, 1, figsize=(14, 5 * n_subplots), sharex=True)
    if n_subplots == 1:
        axes = [axes]

    fig.suptitle("ControllerASG — Análisis de Decisiones", fontsize=14, fontweight="bold")

    # --- Panel 1: Capacidad ---
    ax1 = axes[0]
    cap_times = [t for t, c in zip(times, capacities) if c is not None]
    cap_vals = [c for c in capacities if c is not None]
    ax1.step(cap_times, cap_vals, where="post", color="#37474F", linewidth=1.5, label="Instancias")
    ax1.scatter(times, capacities, c=colors, s=50, zorder=5)
    ax1.set_ylabel("Instancias activas")
    ax1.set_ylim(0, max(cap_vals or [1]) + 1)
    ax1.yaxis.set_major_locator(plt.MaxNLocator(integer=True))
    ax1.grid(True, alpha=0.3)

    # Leyenda de decisiones
    patches = [mpatches.Patch(color=v, label=k.replace("_CAPACITY", "")) for k, v in color_map.items()]
    ax1.legend(handles=patches, loc="upper right", fontsize=8)

    # --- Panel 2: Métricas (si hay datos) ---
    if all_metric_names:
        ax2 = axes[1]
        metric_colors = ["#FF6F00", "#6A1B9A", "#00838F", "#558B2F"]
        for i, name in enumerate(all_metric_names):
            m_times, m_vals = [], []
            for r, t in zip(records, times):
                for m in r.get("Metrics", []):
                    if m.get("Name") == name:
                        m_times.append(t)
                        m_vals.append(m.get("Value", 0))
            if m_times:
                ax2.plot(m_times, m_vals, label=name, color=metric_colors[i % len(metric_colors)], linewidth=1.2, marker="o", markersize=3)
        ax2.set_ylabel("Valor de métrica")
        ax2.legend(loc="upper right", fontsize=8)
        ax2.grid(True, alpha=0.3)

    axes[-1].set_xlabel("Tiempo")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    fig.autofmt_xdate()
    plt.tight_layout()
    plt.savefig(output, dpi=120, bbox_inches="tight")
    print(f"[analyze] Gráfica guardada en: {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analiza decisions.jsonl del ControllerASG")
    parser.add_argument("logfile", help="Ruta al archivo de log (decisions.jsonl)")
    parser.add_argument("--no-plot", action="store_true", help="Solo mostrar texto, no generar imagen")
    parser.add_argument("--output", default="decisions_analysis.png", help="Nombre del archivo de imagen")
    args = parser.parse_args()

    records = load_records(args.logfile)
    print_summary(records)
    if not args.no_plot:
        plot(records, output=args.output)


if __name__ == "__main__":
    main()
