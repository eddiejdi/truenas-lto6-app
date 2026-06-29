#!/usr/bin/env bash
# supervisor.sh — roda orchestrator API + exporter no mesmo container.
# Mantém ambos vivos; se um morrer, derruba o container para o restart policy reagir.
set -euo pipefail

ORCHESTRATOR_PORT="${ORCHESTRATOR_PORT:-9877}"
EXPORTER_PORT="${EXPORTER_PORT:-9125}"

log() { printf '%s [supervisor] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

pids=()
cleanup() {
    log "Encerrando filhos..."
    for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
}
trap cleanup EXIT TERM INT

log "orchestrator_api na :${ORCHESTRATOR_PORT}"
python3 /opt/lto6/orchestrator_api.py &
pids+=("$!")

log "exporter na :${EXPORTER_PORT}"
python3 /opt/lto6/exporter.py &
pids+=("$!")

# Se qualquer filho sair, encerra o container.
wait -n
log "Um processo filho saiu — encerrando container para restart."
exit 1
