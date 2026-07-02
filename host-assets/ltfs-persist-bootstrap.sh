#!/usr/bin/env bash
# Bootstrap idempotente do subsistema LTFS a partir do dataset persistente.
#
# Roda como POSTINIT do TrueNAS (registrado via ltfs-truenas-register.sh) e
# reinstala units/scripts/env que vivem no boot environment — que um upgrade
# do SCALE descarta. A fonte de verdade é /mnt/tank/ltfs-tools, que sobrevive
# a upgrades por estar no pool de dados.
#
# Layout esperado do dataset:
#   /mnt/tank/ltfs-tools/bin/        -> scripts (ltfs_recovery.py, ltfs-fc-stable-start, ...)
#   /mnt/tank/ltfs-tools/systemd/    -> unit files
#   /mnt/tank/ltfs-tools/systemd/units.manifest -> um unit por linha; sufixo " enable" ativa no boot
#   /mnt/tank/ltfs-tools/env/        -> templates de /etc/default/ltfs-*
#   /mnt/tank/ltfs-tools/payload/ltfs-patched.tar.gz -> binários LTFS patcheados (opcional)
set -euo pipefail

DATASET_ROOT="${LTFS_PERSIST_ROOT:-/mnt/tank/ltfs-tools}"
RUNTIME_DIR="${LTFS_RUNTIME_DIR:-/var/db/ltfs-tools}"
PATCHED_DIR="/var/db/ltfs-patched"
LOG_PREFIX="ltfs-persist-bootstrap"

log() { printf "%s [%s] %s\n" "$(date -Iseconds)" "$LOG_PREFIX" "$*"; }

if [[ ! -d "$DATASET_ROOT" ]]; then
  log "ERROR: dataset persistente ausente: $DATASET_ROOT — nada a fazer"
  exit 1
fi

# 1. Scripts do runtime
if [[ -d "$DATASET_ROOT/bin" ]]; then
  mkdir -p "$RUNTIME_DIR"
  cp -a "$DATASET_ROOT/bin/." "$RUNTIME_DIR/"
  log "scripts sincronizados: $DATASET_ROOT/bin -> $RUNTIME_DIR"
fi

# 2. Binários LTFS patcheados (só se ausentes — tarball é pesado)
if [[ ! -x "$PATCHED_DIR/bin/ltfs" && -f "$DATASET_ROOT/payload/ltfs-patched.tar.gz" ]]; then
  mkdir -p "$PATCHED_DIR"
  tar -xzf "$DATASET_ROOT/payload/ltfs-patched.tar.gz" -C "$PATCHED_DIR"
  log "binários LTFS patcheados restaurados em $PATCHED_DIR"
fi

# 3. Env files (não sobrescreve customização local existente)
if [[ -d "$DATASET_ROOT/env" ]]; then
  for tpl in "$DATASET_ROOT"/env/*; do
    [[ -f "$tpl" ]] || continue
    dest="/etc/default/$(basename "$tpl")"
    if [[ ! -f "$dest" ]]; then
      install -m 0644 "$tpl" "$dest"
      log "env file restaurado: $dest"
    fi
  done
fi

# 4. Units systemd conforme manifesto
MANIFEST="$DATASET_ROOT/systemd/units.manifest"
if [[ -f "$MANIFEST" ]]; then
  changed=0
  while read -r unit action; do
    [[ -z "$unit" || "$unit" == \#* ]] && continue
    src="$DATASET_ROOT/systemd/$unit"
    dest="/etc/systemd/system/$unit"
    if [[ ! -e "$src" ]]; then
      log "WARN: unit no manifesto sem arquivo no dataset: $unit"
      continue
    fi
    if ! cmp -s "$src" "$dest" 2>/dev/null; then
      install -D -m 0644 "$src" "$dest"
      changed=1
      log "unit instalada/atualizada: $dest"
    fi
    if [[ "${action:-}" == "enable" ]]; then
      systemctl enable "$unit" >/dev/null 2>&1 || log "WARN: enable falhou para $unit"
    fi
  done < "$MANIFEST"
  [[ "$changed" == 1 ]] && systemctl daemon-reload
fi

# 5. Reconciliação de estado (locks stale, units suspensas, máscaras órfãs)
if [[ -x "$RUNTIME_DIR/ltfs_recovery.py" || -f "$RUNTIME_DIR/ltfs_recovery.py" ]]; then
  set -a; . /etc/default/ltfs-lto6 2>/dev/null || true; set +a
  python3 "$RUNTIME_DIR/ltfs_recovery.py" --boot-reconcile || log "WARN: boot-reconcile retornou erro (não-fatal)"
fi

log "bootstrap concluído"
