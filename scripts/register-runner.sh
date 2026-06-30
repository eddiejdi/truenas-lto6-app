#!/usr/bin/env bash
# register-runner.sh — registra um novo runner self-hosted no homelab
# para o repo eddiejdi/truenas-lto6-app.
#
# Pré-requisito: gh CLI autenticado localmente (gh auth status).
#
# Uso: ./scripts/register-runner.sh
# Execute LOCALMENTE (não no CI) — gera um token de registro e instala
# uma nova instância do runner em /home/homelab/actions-runner-lto6app/ via SSH.

set -euo pipefail

REPO="eddiejdi/truenas-lto6-app"
RUNNER_DIR="/home/homelab/actions-runner-lto6app"
RUNNER_USER="homelab"
HOMELAB="homelab@192.168.15.2"
RUNNER_NAME="homelab-lto6"
RUNNER_LABELS="self-hosted,Linux,X64,homelab"

echo "==> Obtendo token de registro para $REPO"
REG_TOKEN=$(gh api -X POST "/repos/$REPO/actions/runners/registration-token" --jq '.token')
[ -n "$REG_TOKEN" ] || { echo "ERRO: falha ao obter token"; exit 1; }

echo "==> Verificando versão do runner instalado no homelab"
RUNNER_VERSION=$(ssh "$HOMELAB" "ls /home/homelab/actions-runner/bin.2.* 2>/dev/null | tail -1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+$'")
[ -z "$RUNNER_VERSION" ] && RUNNER_VERSION="2.335.1"
echo "    versão: $RUNNER_VERSION"

echo "==> Instalando runner em $HOMELAB:$RUNNER_DIR"
ssh "$HOMELAB" "
  set -e
  mkdir -p $RUNNER_DIR
  # Reutiliza os binários já extraídos do runner principal
  cp -a /home/homelab/actions-runner/. $RUNNER_DIR/
  # Limpar config anterior (se houver)
  if [ -f $RUNNER_DIR/.runner ]; then
    cd $RUNNER_DIR
    ./config.sh remove --token '$REG_TOKEN' 2>/dev/null || true
  fi
  cd $RUNNER_DIR
  ./config.sh \
    --url 'https://github.com/$REPO' \
    --token '$REG_TOKEN' \
    --name '$RUNNER_NAME' \
    --labels '$RUNNER_LABELS' \
    --unattended \
    --replace
  echo 'Runner configurado.'
"

echo "==> Instalando e iniciando o serviço systemd do runner"
ssh "$HOMELAB" "
  cd $RUNNER_DIR
  sudo ./svc.sh install $RUNNER_USER 2>/dev/null || true
  sudo ./svc.sh start
  sudo systemctl is-active actions.runner.eddiejdi-truenas-lto6-app.$RUNNER_NAME.service || true
  echo 'Serviço do runner ativo.'
"

echo ""
echo "==> Runner '$RUNNER_NAME' registrado com sucesso em $REPO"
echo "    Verifique: https://github.com/$REPO/settings/actions/runners"
