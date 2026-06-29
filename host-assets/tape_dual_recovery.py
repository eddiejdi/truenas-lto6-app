#!/usr/bin/env python3
"""
Monitor de recuperação dual LTFS — sg0 e sg1 em paralelo.

Fluxo:
  1. orchestrated-stop nos 2 drives (recolhe as fitas)
  2. self-heal --debug em paralelo com 2 barras de progresso

Uso:
  python3 tools/tape_dual_recovery.py [--host 192.168.15.4] [--skip-stop]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

HOST = "192.168.15.4"
SCRIPT = "/usr/local/tools/ltfs_recovery.py"

# ─── configuração por drive ────────────────────────────────────────────────────
DRIVES = [
    {
        "name": "sg0",
        "label": "LTO-6  sg0  /mnt/tape/lto6",
        "env": {},  # usa defaults do script
    },
    {
        "name": "sg1",
        "label": "LTO-6  sg1  /mnt/tape/lto6-sg1",
        "env": {
            "LTFS_DEVICE": "/dev/sg1",
            "LTFS_TAPE_DEVICE": "/dev/nst1",
            "LTFS_SERVICE": "ltfs-lto6-sg1.service",
            "LTFS_MOUNT_POINT": "/mnt/tape/lto6-sg1",
            "LTFS_ORCH_LOCK": "/run/lock/ltfs-orchestrator-sg1.lock",
        },
    },
]

# ─── mapeamento de marcadores → (percentual, descrição) ───────────────────────
# A primeira entrada cujo marcador aparecer na linha de log avança a barra.
# Ordenado do mais precoce para o mais tardio no fluxo de execução.
STOP_MARKERS: list[tuple[str, int, str]] = [
    ("fusermount",                   15, "Desmontando FUSE"),
    ("list_tape_holders",            30, "Verificando holders"),
    ("Adquirindo",                   40, "Aguardando lock exclusivo"),
    ("stop_conflicting",             55, "Parando serviços conflitantes"),
    ("Operação 'stop' executada",    80, "Serviço parado"),
    ('"success": true',             100, "Recolhida"),
    ('"success": false',            100, "Falhou ao recolher"),
]

HEAL_MARKERS: list[tuple[str, int, str]] = [
    ("[CMD] mountpoint",             8,  "Verificando mountpoint"),
    ("check_catalog",               12,  "Checando catálogo"),
    ("Catálogo LTFS",               14,  "Catálogo acessível"),
    ("Mountpoint LTFS inativo",     16,  "Mount inativo detectado"),
    ("diagnose_known_issue",        22,  "Diagnosticando incidente"),
    ("Diagnosticando",              24,  "Diagnosticando"),
    ("Incidente LTFS conhecido",    34,  "Incidente identificado"),
    ("em cooldown",                 36,  "Verificando cooldown"),
    ("Iniciando operação exclusiva",46,  "Lock exclusivo adquirido"),
    ("stop_conflicting",            50,  "Parando conflitantes"),
    ("CMD-STREAM",                  54,  "Comando exclusivo iniciado"),
    ("[CMD-STREAM] ltfsck",         56,  "ltfsck iniciado"),
    ("LTFS15",                      58,  "ltfsck em progresso"),
    ("Operação 'ltfsck' executada", 82,  "ltfsck concluído"),
    ("[CMD] systemctl reset-failed",85,  "Reset de falha do serviço"),
    ("[CMD] systemctl restart",     87,  "Reiniciando serviço"),
    ("[CMD] systemctl start",       88,  "Iniciando serviço"),
    ("final_check",                 92,  "Verificação final"),
    ("Self-heal LTFS concluído",   100,  "Concluído"),
    ("já está saudável",           100,  "LTFS já saudável"),
    ('"success": true',            100,  "Sucesso"),
    ('"success": false',           100,  "Falhou"),
]

MOUNT_MARKERS: list[tuple[str, int, str]] = [
    ("Iniciando operação exclusiva", 10, "Lock exclusivo adquirido"),
    ("stop_conflicting",             20, "Parando serviços conflitantes"),
    ("ltfs-fc-stable-start",         30, "Iniciando mount LTFS"),
    ("LTFS14000I",                   40, "LTFS iniciando"),
    ("LTFS30209I",                   50, "Abrindo device sg"),
    ("LTFS30250I",                   55, "Device SCSI aberto"),
    ("LTFS11330I",                   60, "Carregando cartridge"),
    ("LTFS14060I",                   75, "Volume montado"),
    ("LTFS12086I",                   85, "Index carregado"),
    ("Operação 'mount' executada",   95, "Mount concluído"),
    ('"success": true',             100, "Montado"),
    ('"success": false',            100, "Falhou"),
]


# ─── estado de progresso por drive ────────────────────────────────────────────
@dataclass
class DriveState:
    name: str
    label: str
    phase: str = "aguardando"
    pct: int = 0
    status: str = "..."
    done: bool = False
    success: Optional[bool] = None
    log_lines: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


# ─── rich ──────────────────────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.live import Live
    from rich.table import Table
    from rich.progress import (
        Progress, BarColumn, TextColumn, TimeElapsedColumn, SpinnerColumn
    )
    from rich.panel import Panel
    from rich.layout import Layout
    RICH = True
except ImportError:
    RICH = False


def _ansi_bar(pct: int, width: int = 40) -> str:
    filled = int(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct:3d}%"


def _render_plain(states: list[DriveState]) -> None:
    """Renderiza progresso simples em terminais sem rich."""
    sys.stdout.write("\033[2J\033[H")  # limpa tela
    print("═" * 60)
    print("  LTFS Dual Recovery Monitor")
    print("═" * 60)
    for s in states:
        with s.lock:
            icon = "✓" if s.success else ("✗" if s.success is False else "…")
            print(f"\n  {icon}  {s.label}")
            print(f"     Fase   : {s.phase}")
            print(f"     Status : {s.status}")
            print(f"     {_ansi_bar(s.pct)}")
    print()
    sys.stdout.flush()


# ─── execução remota via SSH ───────────────────────────────────────────────────
def _ssh_cmd(host: str, env: dict[str, str], args: list[str]) -> list[str]:
    env_prefix = " ".join(f"{k}={v}" for k, v in env.items())
    remote = f"{env_prefix} python3 {SCRIPT} {' '.join(args)}" if env_prefix else f"python3 {SCRIPT} {' '.join(args)}"
    return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", host, remote]


def _run_phase(
    host: str,
    drive: dict,
    state: DriveState,
    args: list[str],
    markers: list[tuple[str, int, str]],
    phase_label: str,
    ltfsck_increment: bool = False,
) -> bool:
    with state.lock:
        state.phase = phase_label
        state.pct = max(state.pct, 2)

    cmd = _ssh_cmd(host, drive["env"], args)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except FileNotFoundError:
        with state.lock:
            state.status = "ssh não encontrado"
            state.done = True
            state.success = False
        return False

    assert proc.stdout is not None
    ltfsck_lines = 0

    for raw in proc.stdout:
        line = raw.rstrip("\n")
        with state.lock:
            state.log_lines.append(line)
            if len(state.log_lines) > 500:
                state.log_lines = state.log_lines[-500:]

        line_lower = line.lower()

        # avanço incremental de ltfsck (LTFS15xxx mensagens)
        if ltfsck_increment and "ltfs15" in line_lower:
            ltfsck_lines += 1
            inc = min(ltfsck_lines * 2, 24)  # até +24% gradual entre 56% e 80%
            with state.lock:
                new_pct = min(56 + inc, 80)
                if new_pct > state.pct:
                    state.pct = new_pct
                    state.status = line[:72].strip() or state.status
            continue

        for marker, pct, desc in markers:
            if marker.lower() in line_lower:
                with state.lock:
                    if pct > state.pct:
                        state.pct = pct
                        state.status = desc
                break

    proc.wait()
    success = proc.returncode == 0

    with state.lock:
        state.pct = 100
        state.done = True
        state.success = success
        if success:
            state.status = "Concluído com sucesso" if phase_label == "recovery" else "Recolhida"
        else:
            state.status = f"Falhou (rc={proc.returncode})"

    return success


# ─── workers por drive ─────────────────────────────────────────────────────────
def _worker(host: str, drive: dict, state: DriveState, skip_stop: bool, mount_mode: bool = False) -> None:
    if not skip_stop:
        state.pct = 1
        _run_phase(host, drive, state, ["--orchestrated-stop"], STOP_MARKERS, "parando", ltfsck_increment=False)
        if not state.success:
            return
        with state.lock:
            state.pct = 0
            state.done = False
            state.success = None

    if mount_mode:
        _run_phase(host, drive, state, ["--orchestrated-mount", "--debug"], MOUNT_MARKERS, "montando", ltfsck_increment=False)
    else:
        _run_phase(host, drive, state, ["--self-heal", "--debug"], HEAL_MARKERS, "recovery", ltfsck_increment=True)


# ─── loop de renderização ──────────────────────────────────────────────────────
def _render_loop_plain(states: list[DriveState]) -> None:
    while not all(s.done for s in states):
        _render_plain(states)
        time.sleep(0.5)
    _render_plain(states)


def _render_loop_rich(states: list[DriveState]) -> None:
    console = Console()

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description:<42}"),
        BarColumn(bar_width=36),
        TextColumn("[cyan]{task.percentage:>3.0f}%"),
        TextColumn("[dim]{task.fields[status]:<32}"),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=4,
        transient=False,
    ) as progress:
        task_ids = [
            progress.add_task(s.label, total=100, status=s.status)
            for s in states
        ]

        while not all(s.done for s in states):
            for tid, s in zip(task_ids, states):
                with s.lock:
                    progress.update(tid, completed=s.pct, status=s.status)
            time.sleep(0.25)

        # atualização final
        for tid, s in zip(task_ids, states):
            with s.lock:
                icon = "✓" if s.success else "✗"
                progress.update(tid, completed=100, status=f"{icon} {s.status}")


# ─── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor dual recovery LTFS sg0+sg1")
    parser.add_argument("--host", default=HOST, help=f"Servidor remoto (padrão: {HOST})")
    parser.add_argument("--skip-stop", action="store_true", help="Pular orchestrated-stop; ir direto à fase de recovery/mount")
    parser.add_argument("--mount", action="store_true", help="Usar orchestrated-mount em vez de self-heal (para fitas paradas e prontas)")
    args = parser.parse_args()

    states = [DriveState(name=d["name"], label=d["label"]) for d in DRIVES]

    threads = [
        threading.Thread(
            target=_worker,
            args=(args.host, DRIVES[i], states[i], args.skip_stop),
            kwargs={"mount_mode": args.mount},
            daemon=True,
        )
        for i in range(len(DRIVES))
    ]

    print(f"\n  Conectando em {args.host} — drives: sg0 e sg1")
    if not args.skip_stop:
        print("  Fase 1: orchestrated-stop (recolher fitas)")
    if args.mount:
        print("  Fase mount: orchestrated-mount --debug\n")
    else:
        print("  Fase recovery: self-heal --debug\n")

    for t in threads:
        t.start()

    if RICH:
        _render_loop_rich(states)
    else:
        _render_loop_plain(states)

    for t in threads:
        t.join()

    # resumo final
    print()
    all_ok = all(s.success for s in states)
    for s in states:
        icon = "✓" if s.success else "✗"
        print(f"  {icon}  {s.label}  —  {s.status}")

    if not all_ok:
        print("\n  Últimas linhas de log dos drives com falha:\n")
        for s in states:
            if not s.success:
                print(f"  ── {s.name} ──")
                for line in s.log_lines[-20:]:
                    print(f"    {line}")
        sys.exit(1)

    print("\n  Recovery concluído em ambos os drives.")


if __name__ == "__main__":
    main()
