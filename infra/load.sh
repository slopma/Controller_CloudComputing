#!/usr/bin/env bash
# infra/load.sh — Generador de carga para el experimento del controller.
#
# Ejecuta un experimento de 3 fases diseñado para provocar ScaleUp y ScaleDown:
#   Fase 1 (baseline):     carga baja  → el controller debe MAINTAIN_CAPACITY
#   Fase 2 (alta demanda): carga alta  → el controller debe INCREASE_CAPACITY
#   Fase 3 (descenso):     carga baja  → el controller debe REDUCE_CAPACITY
#
# Requisitos:
#   - hey (https://github.com/rakyll/hey): go install github.com/rakyll/hey@latest
#   - O ab (Apache Bench): sudo apt install apache2-utils
#   - infra/state.env con LB_DNS relleno (generado por up.sh)
#
# Uso:
#   chmod +x infra/load.sh
#   ./infra/load.sh
#
# Variables de entorno opcionales:
#   TARGET_URL  URL destino (por defecto http://$LB_DNS/)
#   USE_AB      Si =1, usa 'ab' en vez de 'hey'
set -euo pipefail
cd "$(dirname "$0")"

# ---- Cargar IDs de infra ----
[ -f state.env ] || { echo "[load] Error: infra/state.env no existe. Ejecuta ./up.sh primero."; exit 1; }
source state.env

TARGET_URL="${TARGET_URL:-http://${LB_DNS}/}"
USE_AB="${USE_AB:-0}"

echo "============================================="
echo "  Generador de carga — ControllerASG"
echo "  Target: $TARGET_URL"
echo "============================================="

# ---- Verificar herramienta de carga ----
if [ "$USE_AB" = "1" ]; then
    command -v ab >/dev/null || { echo "[load] Error: 'ab' no encontrado. Instala: sudo apt install apache2-utils"; exit 1; }
    TOOL="ab"
else
    command -v hey >/dev/null || {
        echo "[load] 'hey' no encontrado. Intentando instalar con go..."
        go install github.com/rakyll/hey@latest 2>/dev/null && export PATH="$PATH:$(go env GOPATH)/bin" || {
            echo "[load] No se pudo instalar 'hey'. Usa USE_AB=1 para usar Apache Bench."
            exit 1
        }
    }
    TOOL="hey"
fi

# ---- Función de carga ----
send_load() {
    local label="$1"
    local rps="$2"      # requests por segundo
    local duration="$3" # segundos
    local concurrency="${4:-10}"

    echo ""
    echo "[$(date '+%H:%M:%S')] ▶ $label — ${rps} req/s durante ${duration}s (concurrencia: ${concurrency})"

    if [ "$TOOL" = "hey" ]; then
        hey -z "${duration}s" -q "$rps" -c "$concurrency" "$TARGET_URL" 2>&1 | grep -E "^(Summary|Requests|Latency|Status)" || true
    else
        # ab no soporta rate limiting directo; usamos -n grande y -t para duración
        local total=$(( rps * duration ))
        ab -n "$total" -c "$concurrency" -t "$duration" "$TARGET_URL" 2>&1 | grep -E "^(Complete|Failed|Requests per|Time per)" || true
    fi

    echo "[$(date '+%H:%M:%S')] ✓ Fase completada"
}

# ============================================================
#   EXPERIMENTO
# ============================================================

echo ""
echo "Fase 1 — BASELINE (carga baja, 5 min)"
echo "  Objetivo: el controller debe MAINTAIN_CAPACITY"
send_load "Baseline" 10 300 5

echo ""
echo "Fase 2 — CARGA ALTA (10 min)"
echo "  Objetivo: el controller debe INCREASE_CAPACITY"
echo "  (Los umbrales de subida deben superarse: ver scaleUpThreshold en conf.json)"
send_load "Carga alta" 50 600 30

echo ""
echo "Fase 2b — CARGA MUY ALTA (5 min adicionales para forzar ScaleUp adicional)"
send_load "Carga muy alta" 100 300 50

echo ""
echo "Pausa de 2 min — esperando que el controller tome decisiones de scale-down..."
sleep 120

echo ""
echo "Fase 3 — DESCENSO (carga baja, 15 min)"
echo "  Objetivo: el controller debe REDUCE_CAPACITY"
echo "  (Cooldown + confirmCyclesDown alargan el tiempo de respuesta)"
send_load "Descenso" 5 900 3

echo ""
echo "============================================="
echo "  Experimento completado."
echo "  Analiza los resultados con:"
echo "    cat decisions.jsonl | jq '{t:.Time, d:.Decision, r:.Reason, cap:.CapacityBefore.Total}'"
echo "    python3 analysis/analyze.py decisions.jsonl"
echo "============================================="
