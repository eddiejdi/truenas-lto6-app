#!/usr/bin/env bash
# installer.sh — init do container control-plane.
#   1. Valida o buffer pré-tape (PRÉ-REQUISITO — falha cedo)
#   2. Instala binários LTFS + tooling + units no host (via nsenter), se habilitado
#   3. exec → supervisor (orchestrator API + button-watch + exporter)
set -euo pipefail

log() { printf '%s [installer] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
die() { printf '%s [installer][ERRO] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >&2; exit "${2:-1}"; }

BUFFER_PATH="${BUFFER_PATH:?BUFFER_PATH obrigatório}"
BUFFER_MIN_FREE_GIB="${BUFFER_MIN_FREE_GIB:-30}"
INSTALL_HOST_UNITS="${INSTALL_HOST_UNITS:-true}"
LTFS_DEVICE="${LTFS_DEVICE:-/dev/sg4}"
LTFS_TAPE_DEVICE="${LTFS_TAPE_DEVICE:-/dev/nst1}"
LTFS_MOUNT_POINT="${LTFS_MOUNT_POINT:-/mnt/tape/lto6}"
DRIVE_ID="${DRIVE_ID:-lto6}"

# ── 1. PRÉ-REQUISITO: BUFFER PRÉ-TAPE ─────────────────────────────────
log "Validando buffer pré-tape: $BUFFER_PATH"
[ -d "$BUFFER_PATH" ] || die "Buffer pré-tape ausente: $BUFFER_PATH (dataset não montado?)" 90

free_gib="$(df -BG --output=avail "$BUFFER_PATH" 2>/dev/null | tail -1 | tr -dc '0-9')" \
    || die "Não foi possível ler espaço livre de $BUFFER_PATH" 91
[ -n "$free_gib" ] || die "df não retornou espaço livre para $BUFFER_PATH" 91

if [ "$free_gib" -lt "$BUFFER_MIN_FREE_GIB" ]; then
    die "Buffer cheio: ${free_gib}GiB livres < mínimo ${BUFFER_MIN_FREE_GIB}GiB. Libere espaço antes de instalar." 92
fi
log "Buffer OK: ${free_gib}GiB livres (mínimo ${BUFFER_MIN_FREE_GIB}GiB)"

# ── 2. INSTALAÇÃO NO HOST (control-plane via nsenter) ─────────────────
host() { nsenter -t 1 -m -u -i -n -p -- "$@"; }

if [ "$INSTALL_HOST_UNITS" = "true" ]; then
    log "Instalando tooling e units no host via nsenter..."

    # Binários LTFS patcheados: copiar do host só se ausentes (não redistribuímos).
    if ! host test -x /var/db/ltfs-patched/bin/ltfs; then
        log "AVISO: /var/db/ltfs-patched/bin/ltfs ausente no host. O mount LTFS exige os" \
            "binários patcheados. Instale-os no host antes de montar (fora do escopo da imagem)."
    fi

    # Tooling de recovery + orchestrator
    host install -D -m 0755 /proc/1/root/opt/lto6/ltfs_recovery.py /usr/local/tools/ltfs_recovery.py 2>/dev/null \
        || cp /opt/lto6/ltfs_recovery.py /host-install-tmp/ 2>/dev/null || true

    # Como nsenter compartilha o mount namespace do host, /opt/lto6 do container
    # não é visível ao host. Copiamos via /proc/1/root ou bind. Estratégia robusta:
    # escrever em um diretório bind-montado e instalar de lá.
    STAGE=/run/lto6-install
    mkdir -p "$STAGE"
    cp -a /opt/lto6/ltfs_recovery.py "$STAGE/"
    cp -a /opt/lto6/host-install/. "$STAGE/host-install/"
    cp -a /opt/lto6/ltfs_button_watch.py "$STAGE/"

    host bash -c "
        set -e
        S=/run/lto6-install
        install -D -m 0755 \$S/ltfs_recovery.py            /usr/local/tools/ltfs_recovery.py
        install -D -m 0755 \$S/ltfs_button_watch.py        /var/db/ltfs-tools/ltfs_button_watch.py
        install -D -m 0755 \$S/host-install/ltfs-fc-stable-start /usr/local/sbin/ltfs-fc-stable-start
        install -D -m 0755 \$S/host-install/ltfs-lto6-stop      /usr/local/sbin/ltfs-lto6-stop
        install -D -m 0755 \$S/host-install/lto6-resolve-device  /usr/local/sbin/lto6-resolve-device
        install -D -m 0755 \$S/host-install/lto6b-resolve-device /usr/local/sbin/lto6b-resolve-device
        for u in \$S/host-install/systemd/*.service; do
            [ -f \"\$u\" ] && install -D -m 0644 \"\$u\" /etc/systemd/system/\$(basename \"\$u\")
        done
        systemctl daemon-reload
    " || log "AVISO: instalação de units no host retornou erro (verifique privilégios/nsenter)"

    # Env file do drive (sem segredos — segredos vão por env do container)
    host bash -c "
        cat > /etc/default/ltfs-${DRIVE_ID} <<EOF
LTFS_BIN=/var/db/ltfs-patched/bin/ltfs
LTFS_DEVICE=${LTFS_DEVICE}
LTFS_TAPE_DEVICE=${LTFS_TAPE_DEVICE}
LTFS_MOUNT_POINT=${LTFS_MOUNT_POINT}
LTFS_SERVICE=ltfs-${DRIVE_ID}.service
LTFS_ORCH_LOCK=/run/lock/ltfs-orchestrator.lock
EOF
    " || log "AVISO: não foi possível escrever /etc/default/ltfs-${DRIVE_ID}"

    if [ "${AUTO_EJECT_ON_BUTTON:-true}" = "true" ] || [ "${AUTO_MOUNT_ON_INSERT:-true}" = "true" ]; then
        host systemctl enable --now ltfs-button-watch.service 2>/dev/null \
            || log "AVISO: não foi possível ativar ltfs-button-watch.service"
    fi

    log "Instalação no host concluída."
else
    log "INSTALL_HOST_UNITS=false — pulando instalação no host (modo somente-monitoramento)."
fi

# ── 3. SUPERVISOR ─────────────────────────────────────────────────────
log "Iniciando supervisor (orchestrator API + exporter)..."
exec /opt/lto6/supervisor.sh
