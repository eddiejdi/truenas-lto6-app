#!/usr/bin/env bash
# extract-host-assets.sh — extrai (read-only) o tooling LTFS do NAS para host-assets/.
# NÃO escreve no servidor. Sanitiza env files (remove segredos).
#
# Uso: NAS=root@192.168.15.4 ./scripts/extract-host-assets.sh
set -euo pipefail

NAS="${NAS:-root@192.168.15.4}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$ROOT/host-assets"
cd "$ROOT"

mkdir -p "$DST/systemd" "$DST/env"

echo "== Extraindo tooling de $NAS (read-only) =="
for f in ltfs_recovery.py ltfs-fc-stable-start ltfs-lto6-stop \
         lto6-resolve-device lto6b-resolve-device tape_dual_recovery.py; do
  ssh "$NAS" "cat /var/db/ltfs-tools/$f" > "$DST/$f" \
    && echo "  OK $f ($(wc -l < "$DST/$f") linhas)"
done

echo "== Extraindo env files SANITIZADOS (sem segredos) =="
for env in ltfs-lto6 ltfs-lto6b ltfs-recovery; do
  ssh "$NAS" "cat /etc/default/$env" \
    | grep -vE 'TOKEN|PASSWORD|SECRET|CHAT_ID' > "$DST/env/$env.env" \
    && echo "  OK env/$env.env"
done

echo "== Verificação anti-segredo =="
if grep -rIEn '[0-9]{8,10}:[A-Za-z0-9_-]{30,}' "$DST" >/dev/null 2>&1; then
  echo "  ALERTA: possível token real extraído — abortar e revisar!" >&2
  exit 1
fi
echo "  limpo ✓"

echo "== Copiando units versionados (do repo eddie-auto-dev) =="
SRC_REPO="${SRC_REPO:-../eddie-auto-dev}"
for u in ltfs-lto6.service ltfs-button-watch.service; do
  [ -f "$SRC_REPO/systemd/$u" ] && cp "$SRC_REPO/systemd/$u" "$DST/systemd/" && echo "  OK $u"
done
[ -f "$SRC_REPO/tools/ltfs_button_watch.py" ] \
  && cp "$SRC_REPO/tools/ltfs_button_watch.py" "images/lto6-control-plane/rootfs/" \
  && echo "  OK ltfs_button_watch.py → rootfs"

echo "Extração concluída em $DST"
