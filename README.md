# truenas-lto6-app

App de catálogo do **TrueNAS SCALE 24.10 (Electric Eel)** que empacota **instalação,
suporte e monitoramento de fitas LTO-6 (LTFS)** — com **área de buffer pré-tape
obrigatória** e dashboards no padrão dos projetos RPA4ALL.

> Projetado para o NAS Optiplex (`192.168.15.4`). Arquitetura **control-plane**: o mount
> LTFS FUSE roda como serviço systemd no host (robusto com a HBA FC); o app é o plano de
> controle (orchestrator, button-watch, exporter, dashboards).

## Funcionalidades

- **Auto-mount** ao inserir uma fita e **auto-eject** ao pressionar o botão físico do drive
  (via `ltfs_button_watch.py` + orchestrator).
- **Mount/eject orquestrado** com lock exclusivo e self-heal (reusa `ltfs_recovery.py`).
- **Buffer pré-tape como pré-requisito**: o deploy falha se o dataset de buffer não existir
  ou estiver acima do gate (default 80% / abort 88% / mínimo 30 GiB livres).
- **Exporter Prometheus** (`:9125`) + **dashboard Grafana** (buffer, mount, serviço, temperatura).
- **Notificações Telegram** opcionais (segredos via `questions.yaml`, nunca versionados).

## Estrutura

```
ix-dev/stable/lto6-tape/      # app de catálogo (item/app/questions/ix_values + template)
images/lto6-control-plane/    # imagem Docker do control-plane (installer, API, exporter)
host-assets/                  # tooling extraído do NAS (sanitizado) — instalado no host
grafana/dashboards/           # dashboard lto6-tape-manager
scripts/extract-host-assets.sh# reextração read-only do servidor
.github/workflows/            # CI de render + build da imagem
```

## Persistência a upgrades do TrueNAS (POSTINIT)

Units e scripts instalados em `/etc/systemd/system` e `/var/db/ltfs-tools` vivem no
boot environment — um upgrade do SCALE cria um BE novo e os descarta. A camada de
persistência resolve isso:

- **`host-assets/ltfs-persist-bootstrap.sh`** — bootstrap idempotente que reinstala
  scripts/units/env a partir do dataset persistente `/mnt/tank/ltfs-tools`
  (conforme `host-assets/systemd/units.manifest`) e roda `ltfs_recovery.py
  --boot-reconcile` ao final (limpa locks stale, restaura units suspensas, alerta
  cursores órfãos — nunca toca na fita).
- **`host-assets/ltfs-truenas-register.sh`** — registra o bootstrap como POSTINIT via
  `midclt call initshutdownscript.create`; o registro vive no config DB do TrueNAS,
  que migra entre boot environments.
- **`host-assets/ltfs-nfs-share.sh`** — export NFS do mount LTFS via API
  `sharing.nfs.*` (convive com shares gerenciados; não sobrescreve `/etc/exports`).

Setup único na NAS:

```bash
zfs create tank/ltfs-tools
mkdir -p /mnt/tank/ltfs-tools/{bin,systemd,env,payload}
# popular bin/ com host-assets/*.{sh,py} + scripts do runtime, systemd/ com units + manifest,
# env/ com /etc/default/ltfs-*, payload/ com tar de /var/db/ltfs-patched
/mnt/tank/ltfs-tools/bin/ltfs-truenas-register.sh
```

Aplicado na NAS em 2026-07-02 (initshutdownscript id=1); teste de upgrade simulado validado.

## Instalação

### Caminho 1 — Custom App (validação imediata)
TrueNAS → **Apps → Discover → Custom App** → cole `docker-compose.custom-app.yaml`
(ajuste device, buffer e mount point). Aparece em *Installed*.

### Caminho 2 — App Store / Discover (catálogo)
Renderiza `ix-dev/` para um *train* local e injeta no catálogo `TRUENAS`
(`/mnt/.ix-apps/truenas_catalog`) com `midclt call catalog.sync`. Aparece em *Discover*.
Ver `docs/truenas-lto6-app-design.md` (seção 5) no repo `eddie-auto-dev`.

## Pré-requisitos no host

- Binários LTFS patcheados em `/var/db/ltfs-patched/bin/` (ltfs, ltfsck, mkltfs).
- Dataset de buffer pré-tape (ex.: `tank/pretape/lto6-cache`).
- Drive LTO-6 acessível via `/dev/sg*` + `/dev/nst*`.

## Segurança

- Segredos (Telegram) **nunca** no git — vão por `questions.yaml` (`private: true`).
- Deploy no NAS só via pipeline com guardrail; nunca SCP direto.
- Operações de fita sempre via orchestrator (lock exclusivo).
