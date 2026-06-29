# Changelog — LTO-6 Tape Manager

## 1.0.0 — 2026-06-29
- Versão inicial.
- Control-plane LTFS para TrueNAS 24.10 (Electric Eel).
- Auto-mount por inserção e auto-eject por botão físico via `ltfs_button_watch.py`.
- Buffer pré-tape como pré-requisito obrigatório (gate 80% / abort 88% / mín 30 GiB).
- Orchestrator API (`:9877`) sobre `ltfs_recovery.py`.
- Exporter Prometheus (`:9125`) + dashboard Grafana.
