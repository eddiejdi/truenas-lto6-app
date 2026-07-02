#!/usr/bin/env bash
# Registra o bootstrap LTFS como POSTINIT script no TrueNAS (rodar UMA vez na NAS).
#
# O registro vive no config DB do TrueNAS (/data/freenas-v1.db), que é migrado
# entre boot environments — é isso que garante que o subsistema de fita
# sobreviva a upgrades do SCALE.
set -euo pipefail

SCRIPT_PATH="${1:-/mnt/tank/ltfs-tools/bin/ltfs-persist-bootstrap.sh}"
COMMENT="LTFS LTO-6 bootstrap (persistente a upgrades)"

if [[ ! -x "$SCRIPT_PATH" ]]; then
  echo "ERROR: bootstrap não encontrado ou não-executável: $SCRIPT_PATH" >&2
  exit 1
fi

existing_id=$(midclt call initshutdownscript.query \
  "[[\"comment\", \"=\", \"$COMMENT\"]]" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r[0]["id"] if r else "")')

payload=$(cat <<EOF
{"type": "SCRIPT", "script": "$SCRIPT_PATH", "when": "POSTINIT", "enabled": true, "timeout": 120, "comment": "$COMMENT"}
EOF
)

if [[ -n "$existing_id" ]]; then
  midclt call initshutdownscript.update "$existing_id" "$payload" >/dev/null
  echo "POSTINIT atualizado (id=$existing_id): $SCRIPT_PATH"
else
  midclt call initshutdownscript.create "$payload" >/dev/null
  echo "POSTINIT registrado: $SCRIPT_PATH"
fi

midclt call initshutdownscript.query '[["comment", "~", "LTFS"]]'
