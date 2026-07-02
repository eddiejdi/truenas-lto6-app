#!/usr/bin/env bash
# Gerencia o export NFS do mount LTFS via API do TrueNAS (sharing.nfs.*).
#
# Substitui o ltfs-nfs-export.service legado, que sobrescrevia /etc/exports
# inteiro e conflitava com shares gerenciados pelo TrueNAS. Aqui o export
# vive no config DB e convive com os demais.
#
# Uso: ltfs-nfs-share.sh enable|disable [mountpoint]
set -euo pipefail

ACTION="${1:?uso: ltfs-nfs-share.sh enable|disable [mountpoint]}"
MOUNTPOINT="${2:-${LTFS_MOUNT_POINT:-/mnt/tape/lto6}}"
NETWORK="${LTFS_NFS_NETWORK:-192.168.15.0/24}"

share_id=$(midclt call sharing.nfs.query \
  "[[\"path\", \"=\", \"$MOUNTPOINT\"]]" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r[0]["id"] if r else "")')

case "$ACTION" in
  enable)
    if ! mountpoint -q "$MOUNTPOINT"; then
      echo "ERROR: $MOUNTPOINT não está montado — export abortado" >&2
      exit 1
    fi
    payload="{\"path\": \"$MOUNTPOINT\", \"networks\": [\"$NETWORK\"], \"enabled\": true, \"comment\": \"LTFS tape export (gerenciado por ltfs-nfs-share.sh)\", \"maproot_user\": \"root\", \"maproot_group\": \"root\"}"
    if [[ -n "$share_id" ]]; then
      midclt call sharing.nfs.update "$share_id" "$payload" >/dev/null
      echo "share NFS atualizado (id=$share_id): $MOUNTPOINT"
    else
      midclt call sharing.nfs.create "$payload" >/dev/null
      echo "share NFS criado: $MOUNTPOINT"
    fi
    midclt call service.control START nfs >/dev/null 2>&1 || midclt call service.start nfs >/dev/null 2>&1 || true
    ;;
  disable)
    if [[ -n "$share_id" ]]; then
      midclt call sharing.nfs.update "$share_id" '{"enabled": false}' >/dev/null
      echo "share NFS desabilitado (id=$share_id): $MOUNTPOINT"
    else
      echo "share NFS inexistente para $MOUNTPOINT — nada a fazer"
    fi
    ;;
  *)
    echo "uso: ltfs-nfs-share.sh enable|disable [mountpoint]" >&2
    exit 2
    ;;
esac
