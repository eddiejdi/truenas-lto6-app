# LTO-6 Control Plane — orquestra LTFS no host TrueNAS.
# Empacota: tooling de recovery, button-watch, orchestrator API e exporter Prometheus.
# Os binários LTFS patcheados são copiados do host (via installer.sh/nsenter) — não
# são redistribuídos na imagem por questões de licença/versão do drive.
FROM debian:bookworm-slim

LABEL org.opencontainers.image.title="LTO-6 Control Plane" \
      org.opencontainers.image.source="https://github.com/eddiejdi/truenas-lto6-app" \
      org.opencontainers.image.description="Control-plane LTFS para TrueNAS — mount/eject orquestrado + monitoramento"

ENV DEBIAN_FRONTEND=noninteractive

# Ferramentas SCSI/fita + python + util-linux (nsenter) + curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip \
        sg3-utils mt-st \
        util-linux \
        curl ca-certificates \
        fuse3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/lto6

# Tooling do control-plane
COPY rootfs/ /opt/lto6/
# Tooling de recovery extraído do host (host-assets na raiz do repo, copiado no build)
COPY host-assets/ltfs_recovery.py        /opt/lto6/ltfs_recovery.py
COPY host-assets/tape_dual_recovery.py   /opt/lto6/tape_dual_recovery.py
COPY host-assets/ltfs-fc-stable-start    /opt/lto6/host-install/ltfs-fc-stable-start
COPY host-assets/ltfs-lto6-stop          /opt/lto6/host-install/ltfs-lto6-stop
COPY host-assets/lto6-resolve-device     /opt/lto6/host-install/lto6-resolve-device
COPY host-assets/lto6b-resolve-device    /opt/lto6/host-install/lto6b-resolve-device
COPY host-assets/systemd/                /opt/lto6/host-install/systemd/
COPY host-assets/env/                    /opt/lto6/host-install/env/

RUN chmod +x /opt/lto6/installer.sh /opt/lto6/supervisor.sh \
    && chmod +x /opt/lto6/host-install/ltfs-fc-stable-start \
                /opt/lto6/host-install/ltfs-lto6-stop \
                /opt/lto6/host-install/lto6-resolve-device \
                /opt/lto6/host-install/lto6b-resolve-device

EXPOSE 9877 9125

# installer.sh valida buffer → instala no host → exec supervisor (api+watch+exporter)
ENTRYPOINT ["/opt/lto6/installer.sh"]
