#!/usr/bin/env python3
"""Ferramenta de diagnóstico e recuperação LTFS acionada por alertas."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time as time_module
import urllib.request
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
LOGGER = logging.getLogger("ltfs-recovery")

# Modo debug: ativado por --debug ou LTFS_DEBUG=1; expõe streaming em tempo real
DEBUG: bool = os.getenv("LTFS_DEBUG", "0").lower() in {"1", "true", "yes", "on"}


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


for env_file in (Path("/etc/default/ltfs-recovery"), Path("/etc/ltfs-catalog.env")):
    _load_env_file(env_file)

LTFS_MOUNT_POINT = Path(os.getenv("LTFS_MOUNT_POINT", "/mnt/tape/lto6"))
LTFS_CURSOR_DIR = Path(os.getenv("LTFS_CURSOR_DIR", "/var/lib/ltfs/cursors"))
BACKUP_ROOT = Path(os.getenv("LTFS_BACKUP_ROOT", "/mnt/raid1/ltfs-cat-backups"))
CATALOG_DB = os.getenv("TAPE_CATALOG_DB", "")
RETENTION_DAYS = int(os.getenv("LTFS_BACKUP_RETENTION_DAYS", "14"))
LTFS_ALLOW_UNMOUNTED_OUTSIDE_WINDOW = os.getenv("LTFS_ALLOW_UNMOUNTED_OUTSIDE_WINDOW", "false").lower() in {"1", "true", "yes", "on"}
LTFS_USAGE_WINDOW_START = os.getenv("LTFS_USAGE_WINDOW_START", "02:00")
LTFS_USAGE_WINDOW_END = os.getenv("LTFS_USAGE_WINDOW_END", "04:00")
LTFS_SERVICE = os.getenv("LTFS_SERVICE", "ltfs-lto6.service")
LTFS_ENABLE_LEGACY_SELFHEAL_SCRIPT = os.getenv("LTFS_ENABLE_LEGACY_SELFHEAL_SCRIPT", "false").lower() in {"1", "true", "yes", "on"}
LTFS_LEGACY_SELFHEAL_SCRIPT = os.getenv("LTFS_LEGACY_SELFHEAL_SCRIPT", "/usr/local/sbin/ltfs-selfheal-remount.sh")
LTFS_DEVICE = os.getenv("LTFS_DEVICE", "/dev/sg0")
LTFS_TAPE_DEVICE = os.getenv("LTFS_TAPE_DEVICE", "/dev/nst0")
LTFS_JOURNAL_LINES = int(os.getenv("LTFS_JOURNAL_LINES", "160"))
LTFS_ORCH_LOCK = Path(os.getenv("LTFS_ORCH_LOCK", "/run/lock/ltfs-orchestrator.lock"))
LTFS_ORCH_LOCK_WAIT_SECONDS = int(os.getenv("LTFS_ORCH_LOCK_WAIT_SECONDS", "0"))
LTFS_SUSPEND_STATE_FILE = Path(os.getenv("LTFS_SUSPEND_STATE_FILE", "/run/ltfs-recovery/suspended-units.json"))
LTFS_SUSPEND_MASK_TIMERS = os.getenv("LTFS_SUSPEND_MASK_TIMERS", "true").lower() in {"1", "true", "yes", "on"}
LTFS_SELF_HEAL_STATE_FILE = Path(os.getenv("LTFS_SELF_HEAL_STATE_FILE", "/var/lib/ltfs/self_heal_state.json"))
LTFS_SELF_HEAL_REMOUNT_COOLDOWN_SECONDS = int(os.getenv("LTFS_SELF_HEAL_REMOUNT_COOLDOWN_SECONDS", "300"))
LTFS_SELF_HEAL_LTFSCK_COOLDOWN_SECONDS = int(os.getenv("LTFS_SELF_HEAL_LTFSCK_COOLDOWN_SECONDS", "1800"))
LTFS_SELF_HEAL_DEEP_RECOVERY_COOLDOWN_SECONDS = int(os.getenv("LTFS_SELF_HEAL_DEEP_RECOVERY_COOLDOWN_SECONDS", "21600"))
LTFS_SELF_HEAL_CURSOR_RECOVERY_COOLDOWN_SECONDS = int(os.getenv("LTFS_SELF_HEAL_CURSOR_RECOVERY_COOLDOWN_SECONDS", "1800"))
LTFS_UNMOUNT_ALLOW_ACTIVE_WRITERS = os.getenv("LTFS_UNMOUNT_ALLOW_ACTIVE_WRITERS", "false").lower() in {"1", "true", "yes", "on"}
LTFS_UNMOUNT_ALLOW_OPEN_CURSOR = os.getenv("LTFS_UNMOUNT_ALLOW_OPEN_CURSOR", "false").lower() in {"1", "true", "yes", "on"}

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
LTFS_TELEGRAM_CONFIRMATION_TIMEOUT = int(os.getenv("LTFS_TELEGRAM_CONFIRMATION_TIMEOUT", "1800"))
LTFS_TELEGRAM_POLL_INTERVAL = int(os.getenv("LTFS_TELEGRAM_POLL_INTERVAL", "15"))

LTFS_CURSOR_VOLSER_FILE = Path(os.getenv("LTFS_CURSOR_VOLSER_FILE", "/var/lib/ltfs/current_volser.txt"))
LTFS_BIN = os.getenv("LTFS_BIN", "/var/db/ltfs-patched/bin/ltfs")
LTFSCK_BIN = os.getenv("LTFSCK_BIN", "/var/db/ltfs-patched/bin/ltfsck")
MKLTFS_BIN = os.getenv("MKLTFS_BIN", "/var/db/ltfs-patched/bin/mkltfs")
LTFS_RO_RECOVERY_MOUNT = Path(os.getenv("LTFS_RO_RECOVERY_MOUNT", "/mnt/tape/lto6-ro-recovery"))
LTFS_LOGFILE = Path(os.getenv("LTFS_LOGFILE", "/var/log/ltfs-lto6.log"))

LTFS_CONFLICT_SERVICES = [
    item.strip()
    for item in os.getenv(
        "LTFS_CONFLICT_SERVICES",
        "tape-safe-eject.service,ltfs-idle-unmount.timer,ltfs-idle-unmount.service,ltfs-cache-flush.timer,ltfs-cache-flush.service,ltfs-udev-mount.service",
    ).split(",")
    if item.strip()
]

LTFS_BACKGROUND_UNITS = [
    item.strip()
    for item in os.getenv(
        "LTFS_BACKGROUND_UNITS",
        "ltfs-cache-flush.timer,ltfs-cache-flush.service,ltfs-idle-unmount.timer,ltfs-idle-unmount.service,lto6-metrics-export.timer,lto6-metrics-export.service",
    ).split(",")
    if item.strip()
]

LTFS_UNMOUNT_BLOCK_UNITS = [
    item.strip()
    for item in os.getenv(
        "LTFS_UNMOUNT_BLOCK_UNITS",
        "ltfs-cache-flush.service,nextcloud-tape-backup.service,nvme-tape-drain.service,lto6-drain-backups.service,tape-backup.service,staged-tape-backup.service",
    ).split(",")
    if item.strip()
]

LTFS_INDEX_FAILURE_PATTERNS = (
    "Cannot write index",
    "failed to generate and write XML data",
    "The medium might be in an inconsistent state",
    "Volume is inconsistent",
    "Cannot locate index",
    "failed to locate to EOD",
    "Cannot seek EOD",
    "medium consistency check failed",
    "Dropping to read-only mode",
)

LTFS_CURSOR_RECOVERY_STATUSES = {"in_progress", "recover_failed", "rollback_failed"}

KNOWN_ISSUES: list[dict[str, Any]] = [
    {
        "id": "eod_missing_deep_recovery",
        "title": "Volume LTFS exige deep recovery",
        "patterns": (
            "EOD of DP(1) is missing",
            "deep recovery operation is required",
            "Use ltfsck with the --deep-recovery option",
        ),
        "recovery_action": "deep_recovery",
        "severity": "critical",
        "explanation": "O mount normal nao converge. A correcao segura e executar ltfsck --deep-recovery com exclusao mutua e timers auxiliares pausados.",
    },
    {
        "id": "media_index_inconsistent",
        "title": "Indice LTFS inconsistente na fita",
        "patterns": (
            "No index found in the index partition",
            "Medium check failed: extra blocks detected",
            "Run ltfsck",
        ),
        "recovery_action": "ltfsck",
        "severity": "critical",
        "explanation": "A fita foi lida, mas o indice LTFS nao bate com a midia. O caso conhecido e executar ltfsck e remontar.",
    },
    {
        "id": "partition_label_inconsistent",
        "title": "Labels LTFS inconsistentes ou truncados",
        "patterns": (
            "Cannot read ANSI label",
            "expected 80 bytes, but received",
            "failed to read partition labels",
            "Failed to read label (-1012)",
        ),
        "recovery_action": "ltfsck",
        "severity": "critical",
        "explanation": "O mount chegou a abrir o drive, mas os labels LTFS lidos da midia estao inconsistentes. O caminho seguro e executar ltfsck e escalar para deep recovery se o problema persistir.",
    },
    {
        "id": "stale_fuse_mount",
        "title": "Mount FUSE residual ou desconectado",
        "patterns": (
            "Transport endpoint is not connected",
            "stale fuse mount",
            "mountpoint LTFS inativo",
        ),
        "recovery_action": "selfheal_remount",
        "severity": "critical",
        "explanation": "O mount existe ou o service ficou num estado quebrado. O caso conhecido e limpar o mount e remontar.",
    },
    {
        "id": "invalid_sync_option",
        "title": "Opcao LTFS/FUSE invalida no wrapper",
        "patterns": (
            "unknown option 'sync_time=",
            'unknown option "sync_time=',
            "sync_time=300",
        ),
        "recovery_action": "manual_config_fix",
        "severity": "critical",
        "explanation": "A build atual do LTFS nao aceita a opcao antiga sync_time separada. Exige ajuste de wrapper, nao so restart.",
    },
    {
        "id": "eod_locate_entity_not_found",
        "title": "EOD missing + LOCATE falhou (SIGKILL mid-write)",
        "patterns": (
            "LOCATE returns Recorded Entity Not Found",
            "Cannot seek EOD: backend locate call failed (-20301)",
            "failed to locate to EOD (-1201)",
        ),
        "recovery_action": "manual_escalation_required",
        "severity": "critical",
        "explanation": (
            "EOD ausente e LOCATE SCSI falha (-20301): o bloco de EOD nunca foi gravado "
            "(causa provavel: SIGKILL durante escrita). ltfsck --deep-recovery nao resolve. "
            "Escalacao manual obrigatoria: "
            "1) --force-mount-ro (salvar dados em RO antes de qualquer escrita); "
            "2) --erase-history (ltfsck --erase-history, pode perder dados apos ultimo sync); "
            "3) mkltfs -f (perde tudo — ultimo recurso)."
        ),
    },
    {
        "id": "mount_missing",
        "title": "LTFS desmontado",
        "patterns": (
            "Mountpoint LTFS inativo",
            "Mountpoint ausente",
            "is not mounted",
        ),
        "recovery_action": "selfheal_remount",
        "severity": "warning",
        "explanation": "O filesystem nao esta disponivel. O caso conhecido e tentar o self-heal de remount.",
    },
]


def _run_command(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if DEBUG:
        LOGGER.debug("[CMD] %s", " ".join(str(c) for c in cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if DEBUG and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                LOGGER.debug("[STDOUT] %s", line)
        if DEBUG and result.stderr.strip():
            for line in result.stderr.strip().splitlines():
                LOGGER.debug("[STDERR] %s", line)
        return result
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _run_command_streaming(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """Executa comando com saída em tempo real via Popen; usa captura silenciosa quando DEBUG=False."""
    if not DEBUG:
        return _run_command(cmd, timeout=timeout)

    LOGGER.debug("[CMD-STREAM] %s", " ".join(str(c) for c in cmd))
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # mescla stderr no stdout para saída ordenada
            text=True,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            print(line, flush=True)  # saída em tempo real no terminal
            stdout_lines.append(line)
        proc.wait(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(stdout_lines), "")
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise exc
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(cmd, 127, "", str(exc))


def _run_orchestration_command(cmd: list[str], streaming: bool = False) -> Dict[str, Any]:
    """Executa comando operacional e retorna payload padronizado."""
    proc = _run_command_streaming(cmd) if streaming else _run_command(cmd)
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def _parse_lsof_output(raw_output: str) -> list[Dict[str, str]]:
    """Converte saída do lsof em registros simples de posse de device."""
    holders: list[Dict[str, str]] = []
    for line in raw_output.splitlines():
        if not line.strip() or line.startswith("COMMAND"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        # Ignora warnings do lsof, que podem vir no stderr e não representam
        # processos segurando os devices de fita.
        if not parts[1].isdigit():
            continue
        holders.append(
            {
                "command": parts[0],
                "pid": parts[1],
                "user": parts[2],
                "line": line.strip(),
            }
        )
    return holders


def _list_tape_holders() -> list[Dict[str, str]]:
    """Lista processos com descritor aberto nos devices de fita."""
    proc = _run_command(["lsof", LTFS_DEVICE, LTFS_TAPE_DEVICE])
    output = "\n".join(
        part for part in ((proc.stdout or "").strip(), (proc.stderr or "").strip()) if part
    )
    return _parse_lsof_output(output)


def _filter_unexpected_holders(
    holders: list[Dict[str, str]],
    allowed_pids: set[int],
    extra_allowed_cmd_tokens: tuple[str, ...] = (),
) -> list[Dict[str, str]]:
    """Filtra holders que não pertencem ao processo atual/orquestrador."""
    allowed_cmd_tokens = (
        "ltfs_recovery.py",
        "ltfsck",
        "ltfs-fc-stable-start",
        "ltfs-lto6-stop",
        *extra_allowed_cmd_tokens,
    )
    unexpected: list[Dict[str, str]] = []
    for holder in holders:
        try:
            holder_pid = int(holder.get("pid", "0"))
        except ValueError:
            holder_pid = -1
        cmd = holder.get("command", "")
        if holder_pid in allowed_pids:
            continue
        if any(token in cmd for token in allowed_cmd_tokens):
            continue
        unexpected.append(holder)
    return unexpected


_EOD_MISSING_PATTERNS = (
    "EOD of DP(1) is missing",
    "deep recovery operation is required",
    "Use ltfsck with the --deep-recovery option",
)


def _ltfsck_needs_deep_recovery(result: Dict[str, Any]) -> bool:
    """Verifica se a saída do ltfsck indica necessidade de --deep-recovery."""
    cmd = result.get("details", {}).get("command_result", {})
    combined = "\n".join([cmd.get("stdout", ""), cmd.get("stderr", "")])
    return any(p.lower() in combined.lower() for p in _EOD_MISSING_PATTERNS)


_XML_PARSE_ERROR_PATTERNS = (
    "Cannot parse index direct from medium (-5000)",
    "XML parser: failed to read from XML stream",
    "failed to read and parse XML data (-5000)",
    "cannot write the index to an invalid position",
)


def _ltfsck_xml_parse_error(result: Dict[str, Any]) -> bool:
    """Detecta falha por índice XML ilegível ou posição inválida — indica necessidade de erase-history."""
    # Aceita resultado de _run_ltfsck, _run_deep_recovery ou _run_exclusive_operation
    cmd = result.get("details", {}).get("command_result", {})
    combined = "\n".join([
        result.get("stdout", ""),
        result.get("stderr", ""),
        cmd.get("stdout", ""),
        cmd.get("stderr", ""),
    ])
    return any(p.lower() in combined.lower() for p in _XML_PARSE_ERROR_PATTERNS)


def _is_eod_missing_in_mount_log(lines: int = 80) -> bool:
    """Detecta EOD ausente no tail do log de mount LTFS — aciona recuperacao automatica via force_mount_no_eod."""
    _eod_patterns = (
        "EOD of IP(0) is missing",
        "EOD of DP(1) is missing",
        "deep recovery operation is required",
        "LTFS17146E",
        "LTFS17147E",
    )
    try:
        content = LTFS_LOGFILE.read_text(errors="replace")
        tail = "\n".join(content.splitlines()[-lines:]).lower()
        return any(p.lower() in tail for p in _eod_patterns)
    except OSError:
        return False


def _ltfsck_was_blocked(result: Dict[str, Any]) -> bool:
    """Retorna True se o ltfsck não chegou a rodar (bloqueado por device ocupado)."""
    cmd = result.get("details", {}).get("command_result", {})
    stdout = cmd.get("stdout", "") or result.get("stdout", "")
    stderr = cmd.get("stderr", "") or result.get("stderr", "")
    # Bloqueado = saída vazia E holders presentes
    if stdout or stderr:
        return False
    holders = result.get("details", {}).get("holders") or result.get("details", {}).get("unexpected")
    return bool(holders) or (not stdout and not stderr)


def _contains_index_failure(text: str) -> bool:
    corpus = text.lower()
    return any(pattern.lower() in corpus for pattern in LTFS_INDEX_FAILURE_PATTERNS)


def _parse_dt(value: str) -> datetime | None:
    """Parse permissivo para timestamps emitidos pelo LTFS."""
    raw = value.strip().replace("T", " ")
    raw = re.sub(r"\s+", " ", raw)
    candidates = [
        raw,
        re.sub(r"\s+([+-]\d{2})(\d{2})$", r"\1:\2", raw),
    ]
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw[:26], fmt)
        except ValueError:
            pass
    return None


def _parse_ltfsck_rollback_points(output: str) -> list[Dict[str, Any]]:
    """Extrai gerações disponíveis da saída de `ltfsck -l/-m`.

    A saída varia entre builds LTFS, então o parser aceita formatos como
    `Generation: 342`, `Gen = 342` e timestamps ISO presentes na mesma linha
    ou nas linhas seguintes do mesmo bloco.
    """
    points: list[Dict[str, Any]] = []
    current: Dict[str, Any] | None = None
    gen_re = re.compile(r"\b(?:generation|gen)\s*(?:=|:)?\s*(\d+)\b", re.IGNORECASE)
    ts_re = re.compile(
        r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\s*[+-]\d{2}:?\d{2})?)"
    )

    for line in output.splitlines():
        gen_match = gen_re.search(line)
        if gen_match:
            if current:
                points.append(current)
            current = {
                "generation": int(gen_match.group(1)),
                "timestamp": None,
                "raw": [line.strip()],
            }
        elif current:
            current.setdefault("raw", []).append(line.strip())

        if current:
            ts_match = ts_re.search(line)
            if ts_match and current.get("timestamp") is None:
                current["timestamp"] = _parse_dt(ts_match.group(1))

    if current:
        points.append(current)

    return points


def _choose_rollback_point(points: list[Dict[str, Any]], cursor_time: datetime | None) -> Dict[str, Any] | None:
    """Escolhe a geração mais nova não posterior ao cursor."""
    if not points:
        return None
    if cursor_time is None:
        return max(points, key=lambda p: int(p.get("generation", -1)))

    timestamped = [p for p in points if p.get("timestamp") is not None]
    eligible = [p for p in timestamped if p["timestamp"] <= cursor_time]
    if eligible:
        return max(eligible, key=lambda p: (p["timestamp"], int(p.get("generation", -1))))
    if timestamped:
        return min(timestamped, key=lambda p: abs((p["timestamp"] - cursor_time).total_seconds()))
    return max(points, key=lambda p: int(p.get("generation", -1)))


def _split_files_by_rollback_point(
    files_written: list[Dict[str, Any]],
    files_pending: list[Dict[str, Any]],
    rollback_point: Dict[str, Any] | None,
) -> tuple[list[Dict[str, Any]], list[Dict[str, Any]]]:
    """Separa arquivos confirmados antes do rollback dos que precisam voltar para fila."""
    if not rollback_point or rollback_point.get("timestamp") is None:
        return files_written, files_pending

    rollback_time = rollback_point["timestamp"]
    recovered: list[Dict[str, Any]] = []
    requeue: list[Dict[str, Any]] = list(files_pending)
    for item in files_written:
        written_at = _parse_dt(str(item.get("written_at", "")))
        if written_at is not None and written_at <= rollback_time:
            recovered.append(item)
        else:
            requeue.append(item)
    return recovered, requeue


def _detect_volser() -> str:
    """Descobre o volser da fita atual: arquivo de estado → journal recente → fallback por device.

    Prioridade:
    1. LTFS_CURSOR_VOLSER_FILE (escrito por tape_orchestrator.py)
    2. Linha "Volume mounted successfully. VOLSER :" no journal do ltfs nas últimas 30 min
    3. Fallback derivado do device para evitar colisão entre drives (ex: UNKNOWN-sg0)
    """
    if LTFS_CURSOR_VOLSER_FILE.exists():
        v = LTFS_CURSOR_VOLSER_FILE.read_text().strip()
        if v:
            return v
    proc = _run_command(["journalctl", "-t", "ltfs", "--since", "30 minutes ago", "--no-pager"])
    for line in reversed((proc.stdout or "").splitlines()):
        m = re.search(r"Volume mounted successfully\.\s+(\w+)\s*:", line)
        if m:
            return m.group(1)
    dev_name = Path(LTFS_DEVICE).name
    return f"UNKNOWN-{dev_name}"


def _stop_ltfs_service_loop(wait_seconds: int = 15) -> None:
    """Para o loop Restart=/OnFailure= do serviço LTFS e aguarda device livre.

    Mascara LTFS_SERVICE e o escalador (ltfs-lto6-selfheal-escalator.service)
    para evitar que o loop de restart interfira com operações de recovery.
    Aguarda até wait_seconds segundos pela liberação do device.
    """
    escalator = os.getenv("LTFS_SELFHEAL_ESCALATOR_SERVICE", "ltfs-lto6-selfheal-escalator.service")
    for svc in (LTFS_SERVICE, escalator):
        _run_orchestration_command(["systemctl", "mask", "--runtime", svc])
        _run_orchestration_command(["systemctl", "stop", svc])
        _run_orchestration_command(["systemctl", "reset-failed", svc])

    deadline = time_module.time() + wait_seconds
    while time_module.time() < deadline:
        holders = _list_tape_holders()
        if not holders:
            return
        LOGGER.info("Aguardando device liberado: %s", [h.get("command") for h in holders])
        time_module.sleep(2)
    LOGGER.warning("Device ainda ocupado após %ds — prosseguindo mesmo assim", wait_seconds)


def _stop_conflicting_services() -> Dict[str, Any]:
    """Para serviços que podem competir com mount/recovery da fita."""
    return _suspend_interfering_units("conflict-preflight", LTFS_CONFLICT_SERVICES)


def _systemd_unit_snapshot(unit: str) -> Dict[str, Any]:
    """Captura o estado original para restaurar somente o que estava ativo."""
    active = _run_command(["systemctl", "is-active", unit])
    enabled = _run_command(["systemctl", "is-enabled", unit])
    active_state = ((active.stdout or active.stderr).strip() or "inactive").splitlines()[0]
    enabled_state = ((enabled.stdout or enabled.stderr).strip() or "unknown").splitlines()[0]
    return {
        "unit": unit,
        "service": unit,
        "active_state": active_state,
        "enabled_state": enabled_state,
        "was_active": active_state in {"active", "activating", "reloading", "deactivating"},
        "was_masked": enabled_state == "masked",
        "is_timer": unit.endswith(".timer"),
    }


def _write_suspension_state(payload: Dict[str, Any]) -> None:
    try:
        LTFS_SUSPEND_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LTFS_SUSPEND_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.rename(LTFS_SUSPEND_STATE_FILE)
    except OSError as exc:
        LOGGER.warning("Falha ao gravar estado de suspensão %s: %s", LTFS_SUSPEND_STATE_FILE, exc)


def _load_suspension_state() -> Dict[str, Any] | None:
    try:
        if not LTFS_SUSPEND_STATE_FILE.exists():
            return None
        return json.loads(LTFS_SUSPEND_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Falha ao ler estado de suspensão %s: %s", LTFS_SUSPEND_STATE_FILE, exc)
        return None


def _suspend_interfering_units(reason: str, units: list[str]) -> Dict[str, Any]:
    """Suspende units interferentes e registra como retornar ao estado anterior."""
    records: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for unit in units:
        if unit in seen:
            continue
        seen.add(unit)
        record = _systemd_unit_snapshot(unit)
        record["stop_result"] = _run_orchestration_command(["systemctl", "stop", unit])
        if LTFS_SUSPEND_MASK_TIMERS and record["is_timer"] and not record["was_masked"]:
            record["mask_result"] = _run_orchestration_command(["systemctl", "mask", "--runtime", unit])
        records.append(record)

    suspension = {
        "reason": reason,
        "suspended_at": datetime.now().isoformat(),
        "state_file": str(LTFS_SUSPEND_STATE_FILE),
        "units": records,
    }
    payload = {
        "suspension": suspension,
        "suspended_units": records,
        "stopped_services": [
            {"service": record["unit"], "result": record.get("stop_result", {})}
            for record in records
        ],
        "state_file": str(LTFS_SUSPEND_STATE_FILE),
    }
    _write_suspension_state(suspension)
    return payload


def _extract_suspension(payload: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not payload:
        return None
    if "suspension" in payload:
        return payload["suspension"]
    if "units" in payload:
        return payload
    if "suspended_units" in payload:
        return {
            "reason": payload.get("reason", "legacy-payload"),
            "suspended_at": payload.get("suspended_at"),
            "state_file": payload.get("state_file", str(LTFS_SUSPEND_STATE_FILE)),
            "units": payload.get("suspended_units", []),
        }
    return None


def _resume_suspended_units(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Restaura apenas units que estavam ativas antes da suspensão."""
    suspension = _extract_suspension(payload) or _load_suspension_state()
    if not suspension:
        return {"resumed_units": [], "started_services": [], "state_file": str(LTFS_SUSPEND_STATE_FILE)}

    resumed: list[Dict[str, Any]] = []
    for record in reversed(suspension.get("units", [])):
        unit = record["unit"]
        resume_record: Dict[str, Any] = {
            "unit": unit,
            "service": unit,
            "was_active": record.get("was_active", False),
            "was_masked": record.get("was_masked", False),
        }
        if record.get("is_timer") and record.get("mask_result") and not record.get("was_masked", False):
            resume_record["unmask_result"] = _run_orchestration_command(["systemctl", "unmask", unit])
        if record.get("was_active") and not record.get("was_masked", False):
            resume_record["start_result"] = _run_orchestration_command(["systemctl", "start", unit])
        resumed.append(resume_record)

    try:
        LTFS_SUSPEND_STATE_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    return {
        "resumed_units": resumed,        "started_services": [
            {"service": record["unit"], "result": record.get("start_result", {})}
            for record in resumed
            if "start_result" in record
        ],
        "state_file": str(LTFS_SUSPEND_STATE_FILE),
    }


def _resume_suspended_unit_sets(*payloads: Dict[str, Any] | None) -> Dict[str, Any]:
    results = [_resume_suspended_units(payload) for payload in payloads if _extract_suspension(payload)]
    if not results:
        results = [_resume_suspended_units(None)]
    return {
        "resume_results": results,
        "started_services": [
            item
            for result in results
            for item in result.get("started_services", [])
        ],
    }


def _pause_background_ltfs_units() -> Dict[str, Any]:
    """Pausa timers/units auxiliares enquanto recovery pesado está em curso."""
    return _suspend_interfering_units("background-recovery", LTFS_BACKGROUND_UNITS)


def _resume_background_ltfs_units(payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """Religa timers/units auxiliares após LTFS voltar a um estado saudável."""
    return _resume_suspended_units(payload)


def _clear_stale_lock(lock_path: Path) -> None:
    """Remove lockfile se o PID gravado nele não existe mais."""
    if not lock_path.exists():
        return
    try:
        m = re.search(r"pid=(\d+)", lock_path.read_text())
        if not m:
            return
        pid = int(m.group(1))
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            LOGGER.warning("Removendo lock stale de PID morto %d: %s", pid, lock_path)
            lock_path.unlink(missing_ok=True)
        except PermissionError:
            pass  # processo existe, pertence a outro uid — não remover
    except (OSError, ValueError):
        pass


@contextmanager
def _exclusive_tape_lock(wait_seconds: int = LTFS_ORCH_LOCK_WAIT_SECONDS):
    """Garante exclusividade de operações de fita via lockfile."""
    LTFS_ORCH_LOCK.parent.mkdir(parents=True, exist_ok=True)
    _clear_stale_lock(LTFS_ORCH_LOCK)
    with LTFS_ORCH_LOCK.open("w", encoding="utf-8") as lock_fd:
        start_time = time_module.time()
        while True:
            try:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if wait_seconds <= 0 or time_module.time() - start_time >= wait_seconds:
                    raise RuntimeError("Lock de fita já está em uso por outro processo") from exc
                time_module.sleep(1)

        try:
            lock_fd.write(f"pid={os.getpid()} started_at={datetime.now().isoformat()}\n")
            lock_fd.flush()
            yield
        finally:
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)


def _run_exclusive_operation(
    operation: str,
    command: list[str],
    streaming: bool = False,
    extra_allowed_cmd_tokens: tuple[str, ...] = (),
    preflight: Any | None = None,
    success_codes: frozenset[int] = frozenset({0}),
) -> Dict[str, Any]:
    """Executa operação exclusiva de fita com preflight anti-concorrência.

    success_codes: conjunto de exit codes considerados sucesso. Use frozenset({0, 1})
    para operações ltfsck onde 0=consistente/sem-mudanças e 1=reparado-com-sucesso.
    """
    LOGGER.info("Iniciando operação exclusiva LTFS: %s", operation)
    if DEBUG:
        LOGGER.debug("[EXCL] device=%s tape=%s lock=%s", LTFS_DEVICE, LTFS_TAPE_DEVICE, LTFS_ORCH_LOCK)
    try:
        with _exclusive_tape_lock():
            if preflight is not None:
                preflight_result = preflight()
                if not preflight_result.get("success", False):
                    return _respond(
                        False,
                        f"Operação '{operation}' bloqueada por política de segurança",
                        {"operation": operation, "preflight": preflight_result},
                    )
            service_actions = _stop_conflicting_services()
            current_holders = _list_tape_holders()
            unexpected = _filter_unexpected_holders(
                current_holders,
                allowed_pids={os.getpid(), os.getppid()},
                extra_allowed_cmd_tokens=extra_allowed_cmd_tokens,
            )
            if unexpected:
                resume_actions = _resume_suspended_units(service_actions)
                return _respond(
                    False,
                    f"Operação '{operation}' bloqueada por concorrência no device",
                    {
                        "operation": operation,
                        "holders": current_holders,
                        "unexpected": unexpected,
                        "resume_after_block": resume_actions,
                        **service_actions,
                    },
                )

            result = _run_orchestration_command(command, streaming=streaming)
            return _respond(
                result["returncode"] in success_codes,
                f"Operação '{operation}' executada",
                {
                    "operation": operation,
                    "command_result": result,
                    **service_actions,
                },
            )
    except RuntimeError as exc:
        return _respond(False, str(exc), {"operation": operation, "lock_file": str(LTFS_ORCH_LOCK)})


def _respond(success: bool, message: str, details: Dict[str, Any] | None = None) -> Dict[str, Any]:
    payload = {
        "success": success,
        "message": message,
        "details": details or {},
        "timestamp": datetime.now().isoformat(),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return payload


def _journal_tail() -> str:
    proc = _run_command(["journalctl", "-u", LTFS_SERVICE, "-n", str(LTFS_JOURNAL_LINES), "--no-pager"])
    return (proc.stdout or proc.stderr or "").strip()


def _load_self_heal_state() -> Dict[str, Any]:
    try:
        if LTFS_SELF_HEAL_STATE_FILE.exists():
            return json.loads(LTFS_SELF_HEAL_STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Falha ao ler state file do self-heal: %s", LTFS_SELF_HEAL_STATE_FILE)
    return {}


def _save_self_heal_state(state: Dict[str, Any]) -> None:
    try:
        LTFS_SELF_HEAL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LTFS_SELF_HEAL_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except OSError:
        LOGGER.warning("Falha ao gravar state file do self-heal: %s", LTFS_SELF_HEAL_STATE_FILE)


def _action_cooldown_seconds(action: str) -> int:
    return {
        "selfheal_remount": LTFS_SELF_HEAL_REMOUNT_COOLDOWN_SECONDS,
        "ltfsck": LTFS_SELF_HEAL_LTFSCK_COOLDOWN_SECONDS,
        "deep_recovery": LTFS_SELF_HEAL_DEEP_RECOVERY_COOLDOWN_SECONDS,
        "cursor_recover": LTFS_SELF_HEAL_CURSOR_RECOVERY_COOLDOWN_SECONDS,
        "erase_history_telegram": 3600,
    }.get(action, 0)


def _action_in_cooldown(action: str, now: datetime | None = None) -> Dict[str, Any] | None:
    state = _load_self_heal_state()
    actions = state.get("actions", {})
    action_state = actions.get(action, {})
    last_attempt = action_state.get("last_attempt_at")
    if not last_attempt:
        return None

    try:
        last_attempt_at = datetime.fromisoformat(last_attempt)
    except ValueError:
        return None

    cooldown = _action_cooldown_seconds(action)
    elapsed = int(((now or datetime.now()) - last_attempt_at).total_seconds())
    if elapsed >= cooldown:
        return None

    return {
        "action": action,
        "last_attempt_at": last_attempt,
        "cooldown_seconds": cooldown,
        "elapsed_seconds": elapsed,
        "remaining_seconds": max(cooldown - elapsed, 0),
        "last_result_success": action_state.get("last_result_success"),
    }


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _telegram_send(text: str) -> bool:
    """Envia mensagem de texto ao Telegram. Retorna True se enviou com sucesso."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        LOGGER.warning("Telegram não configurado (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes)")
        return False
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:
        LOGGER.warning("Falha ao enviar Telegram: %s", exc)
        return False


def _telegram_ask_yn(question: str, timeout_s: int = LTFS_TELEGRAM_CONFIRMATION_TIMEOUT) -> bool | None:
    """Envia pergunta YES/NO via Telegram inline keyboard.
    Retorna True (sim), False (não) ou None (timeout/indisponível).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        LOGGER.warning("Telegram não configurado — erase-history requer confirmação manual, abortando")
        return None

    keyboard = [[
        {"text": "✅ SIM — executar", "callback_data": "ltfs_yes"},
        {"text": "❌ NÃO — abortar", "callback_data": "ltfs_no"},
    ]]
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": question,
        "reply_markup": {"inline_keyboard": keyboard},
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            msg_id = json.loads(resp.read()).get("result", {}).get("message_id")
            if not msg_id:
                return None
    except Exception as exc:
        LOGGER.warning("Telegram sendMessage falhou: %s", exc)
        return None

    # Determina offset inicial para ignorar updates antigos
    try:
        with urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?limit=1&offset=-1",
            timeout=10,
        ) as resp:
            updates = json.loads(resp.read()).get("result", [])
            offset = updates[-1]["update_id"] + 1 if updates else 0
    except Exception:
        offset = 0

    deadline = time_module.time() + timeout_s
    LOGGER.info("Aguardando confirmação Telegram (timeout %ds)…", timeout_s)

    while time_module.time() < deadline:
        poll_secs = min(LTFS_TELEGRAM_POLL_INTERVAL, max(1, int(deadline - time_module.time())))
        try:
            url = (
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
                f"/getUpdates?offset={offset}&timeout={poll_secs}"
            )
            with urllib.request.urlopen(url, timeout=poll_secs + 5) as resp:
                updates = json.loads(resp.read()).get("result", [])
        except Exception:
            time_module.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            cb = update.get("callback_query", {})
            if not cb:
                continue
            # Responde ao callback para remover o "loading" no botão
            try:
                ack = json.dumps({"callback_query_id": cb.get("id", "")}).encode()
                ack_req = urllib.request.Request(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                    data=ack,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(ack_req, timeout=5)
            except Exception:
                pass
            data = cb.get("data", "")
            if data == "ltfs_yes":
                return True
            if data == "ltfs_no":
                return False

    _telegram_send(f"⏰ Timeout de {timeout_s // 60} min atingido sem resposta. Operação abortada.")
    return None


def _record_action_attempt(action: str, success: bool, details: Dict[str, Any] | None = None, now: datetime | None = None) -> None:
    state = _load_self_heal_state()
    actions = state.setdefault("actions", {})
    actions[action] = {
        "last_attempt_at": (now or datetime.now()).isoformat(),
        "last_result_success": success,
        "details": details or {},
    }
    _save_self_heal_state(state)


def _collect_runtime_state(now: datetime | None = None) -> Dict[str, Any]:
    checked_at = (now or datetime.now()).isoformat()
    mount_expected = _is_mount_expected(now)
    mount_exists = LTFS_MOUNT_POINT.exists()
    mount_cmd = _run_command(["mountpoint", "-q", str(LTFS_MOUNT_POINT)]) if mount_exists else subprocess.CompletedProcess([], 1, "", "mountpoint absent")
    is_mounted = mount_exists and mount_cmd.returncode == 0

    systemctl_active = _run_command(["systemctl", "is-active", LTFS_SERVICE])
    service_state = (systemctl_active.stdout or systemctl_active.stderr).strip() or "unknown"

    journal = _journal_tail()
    df = _run_command(["df", "-h", str(LTFS_MOUNT_POINT)]) if is_mounted else subprocess.CompletedProcess([], 1, "", "")

    return {
        "checked_at": checked_at,
        "mountpoint": str(LTFS_MOUNT_POINT),
        "mount_expected": mount_expected,
        "mount_exists": mount_exists,
        "mounted": is_mounted,
        "mount_stderr": mount_cmd.stderr.strip() if hasattr(mount_cmd, "stderr") else "",
        "service": LTFS_SERVICE,
        "service_state": service_state,
        "ltfs_device": LTFS_DEVICE,
        "journal_excerpt": journal,
        "df": df.stdout.strip(),
    }


def _service_is_thrashing(state: Dict[str, Any]) -> bool:
    service_state = (state.get("service_state") or "").strip().lower()
    journal = state.get("journal_excerpt", "").lower()
    if service_state in {"failed", "activating", "deactivating", "auto-restart"}:
        return True
    if service_state == "active":
        return False
    # "inactive" pode ser um loop recém parado: checar journal por falhas recentes.
    # Isso permite que o self-heal fora da janela intervenha quando o serviço
    # estava ciclando mas foi parado (ex: manualmente ou por StartLimitBurst).
    recent_failure_markers = (
        "scheduled restart job",
        "failed with result 'exit-code'",
        "failed to start",
        "medium check failed",
        "extra blocks detected",
        "cannot mount the volume",
    )
    return any(marker in journal for marker in recent_failure_markers)


def _should_intervene_outside_window(state: Dict[str, Any]) -> bool:
    return not state.get("mount_expected", True) and _service_is_thrashing(state)


def _cursor_recovery_issue(
    state: Dict[str, Any],
    open_cursors: list[Dict[str, Any]] | None = None,
) -> Dict[str, Any] | None:
    """Detecta sessão de escrita interrompida que exige recuperação por cursor."""
    cursors = open_cursors if open_cursors is not None else _list_recovery_cursors()
    if not cursors:
        return None
    if state.get("mounted") and not _contains_index_failure(state.get("journal_excerpt", "")):
        return None
    return {
        "id": "open_cursor_requires_recovery",
        "title": "Cursor de escrita aberto com LTFS desmontado ou inconsistente",
        "severity": "critical",
        "recovery_action": "cursor_recover",
        "explanation": (
            "Há uma sessão de escrita sem fechamento limpo. O caminho seguro é "
            "executar recovery por cursor para restaurar o último índice persistido "
            "e refileirar a cauda não confirmada."
        ),
    }


def diagnose_known_issue(now: datetime | None = None) -> Dict[str, Any]:
    state = _collect_runtime_state(now=now)
    if DEBUG:
        LOGGER.debug("[DIAGNOSE] service=%s mounted=%s expected=%s", state.get("service_state"), state.get("mounted"), state.get("mount_expected"))
        LOGGER.debug("[DIAGNOSE] journal_lines=%d", len((state.get("journal_excerpt") or "").splitlines()))
    corpus = "\n".join(
        [
            state.get("journal_excerpt", ""),
            state.get("mount_stderr", ""),
            state.get("service_state", ""),
        ]
    )

    open_cursors = _list_recovery_cursors()
    matched: dict[str, Any] | None = _cursor_recovery_issue(state, open_cursors)
    if matched is None:
        for issue in KNOWN_ISSUES:
            if any(pattern.lower() in corpus.lower() for pattern in issue["patterns"]):
                matched = issue
                break

    if matched is None and not state["mounted"] and state["mount_expected"]:
        matched = next(issue for issue in KNOWN_ISSUES if issue["id"] == "mount_missing")

    details = {
        "state": state,
        "issue": None,
        "known_issue": False,
        "open_cursors": open_cursors,
    }
    if matched is None:
        return _respond(False, "Nenhuma assinatura conhecida de incidente LTFS encontrada", details)

    issue_details = {
        "id": matched["id"],
        "title": matched["title"],
        "severity": matched["severity"],
        "recovery_action": matched["recovery_action"],
        "explanation": matched["explanation"],
    }
    details["issue"] = issue_details
    details["known_issue"] = True
    return _respond(True, f"Incidente LTFS conhecido detectado: {matched['title']}", details)


def _run_selfheal_script() -> Dict[str, Any]:
    if LTFS_ENABLE_LEGACY_SELFHEAL_SCRIPT and Path(LTFS_LEGACY_SELFHEAL_SCRIPT).exists():
        proc = _run_command([LTFS_LEGACY_SELFHEAL_SCRIPT])
    else:
        proc = _run_command(["systemctl", "restart", LTFS_SERVICE])
    return {
        "returncode": proc.returncode,
        "stdout": (proc.stdout or "").strip(),
        "stderr": (proc.stderr or "").strip(),
    }


def _run_ltfsck() -> Dict[str, Any]:
    result = _run_exclusive_operation("ltfsck", ["ltfsck", "-f", LTFS_DEVICE], streaming=True, success_codes=frozenset({0, 1}))
    details = result.get("details", {})
    command_result = details.get("command_result", {})
    return {
        "returncode": command_result.get("returncode", 1),
        "stdout": command_result.get("stdout", ""),
        "stderr": command_result.get("stderr", ""),
        "details": details,
        "paused_units": details,
    }


def _run_deep_recovery() -> Dict[str, Any]:
    paused_units = _pause_background_ltfs_units()
    result = deep_recovery()
    details = result.get("details", {})
    command_result = details.get("command_result", {})
    return {
        "returncode": command_result.get("returncode", 1),
        "stdout": command_result.get("stdout", ""),
        "stderr": command_result.get("stderr", ""),
        "details": details,
        "paused_units": paused_units,
        "exclusive_paused_units": details,
    }


def _recovery_action_succeeded(action_result: Dict[str, Any], ltfsck_op: bool = False) -> bool:
    if "success" in action_result:
        return bool(action_result.get("success"))
    try:
        rc = int(action_result.get("returncode", 1))
        # ltfsck retorna 0=consistente-sem-mudanças, 1=reparado-com-sucesso, 4=falha-grave
        return rc in (0, 1) if ltfsck_op else rc == 0
    except (TypeError, ValueError):
        return False


def _execute_recovery_action(action: str) -> Dict[str, Any]:
    action_result: Dict[str, Any]
    resume_background_units = False

    if action == "selfheal_remount":
        action_result = _run_selfheal_script()
    elif action == "ltfsck":
        action_result = _run_ltfsck()
        if action_result["returncode"] in {0, 1}:
            # Se há cursor em aberto, cursor_recover atualiza seu estado e reinicia o serviço.
            # Caso contrário, reinicia diretamente.
            volser = _detect_volser()
            cursor_data, _ = _cursor_read(volser)
            if cursor_data and cursor_data.get("status") not in ("clean", None):
                LOGGER.info("Cursor em aberto para %s — executando cursor_recover após ltfsck", volser)
                cr = cursor_recover(volser)
                action_result["cursor_recover"] = cr
                if not cr.get("success"):
                    action_result["returncode"] = 1
            else:
                restart_result = _run_command(["systemctl", "restart", LTFS_SERVICE])
                action_result["post_restart"] = {
                    "returncode": restart_result.returncode,
                    "stdout": (restart_result.stdout or "").strip(),
                    "stderr": (restart_result.stderr or "").strip(),
                }
                resume_background_units = restart_result.returncode == 0
    elif action == "deep_recovery":
        action_result = _run_deep_recovery()
        if action_result["returncode"] in {0, 1}:
            reset_result = _run_command(["systemctl", "reset-failed", LTFS_SERVICE])
            start_result = _run_command(["systemctl", "start", LTFS_SERVICE])
            action_result["post_restart"] = {
                "reset_failed": {
                    "returncode": reset_result.returncode,
                    "stdout": (reset_result.stdout or "").strip(),
                    "stderr": (reset_result.stderr or "").strip(),
                },
                "start": {
                    "returncode": start_result.returncode,
                    "stdout": (start_result.stdout or "").strip(),
                    "stderr": (start_result.stderr or "").strip(),
                },
            }
            resume_background_units = start_result.returncode == 0
    elif action == "erase_history_telegram":
        question = (
            f"⚠️ LTFS Recovery — fita {LTFS_DEVICE}\n\n"
            "ltfsck e deep-recovery falharam. O índice da fita está corrompido "
            "e precisa ser APAGADO e reconstruído do zero (--erase-history).\n\n"
            "ATENÇÃO: esta operação é IRREVERSÍVEL e pode causar perda de "
            "arquivos não referenciados no índice.\n\n"
            "Confirma execução de ltfsck --erase-history?"
        )
        confirmed = _telegram_ask_yn(question, timeout_s=LTFS_TELEGRAM_CONFIRMATION_TIMEOUT)
        if confirmed is None:
            action_result = {
                "success": False,
                "returncode": 1,
                "message": "erase-history aguardando confirmação Telegram — timeout sem resposta",
                "details": {"reason": "telegram_timeout"},
            }
        elif not confirmed:
            action_result = {
                "success": False,
                "returncode": 1,
                "message": "erase-history recusado pelo operador via Telegram",
                "details": {"reason": "operator_rejected"},
            }
        else:
            _telegram_send(f"✅ Confirmado. Iniciando ltfsck --erase-history em {LTFS_DEVICE}…")
            eh_result = erase_history()
            action_result = {
                "success": eh_result.get("success", False),
                "returncode": 0 if eh_result.get("success") else 1,
                "message": eh_result.get("message", ""),
                "stdout": eh_result.get("details", {}).get("command_result", {}).get("stdout", ""),
                "stderr": eh_result.get("details", {}).get("command_result", {}).get("stderr", ""),
                "details": eh_result.get("details", {}),
            }
            if action_result["success"]:
                reset_result = _run_command(["systemctl", "reset-failed", LTFS_SERVICE])
                start_result = _run_command(["systemctl", "start", LTFS_SERVICE])
                action_result["post_restart"] = {
                    "reset_failed": {"returncode": reset_result.returncode},
                    "start": {"returncode": start_result.returncode},
                }
                resume_background_units = start_result.returncode == 0
                if resume_background_units:
                    _telegram_send(f"✅ erase-history concluído. Serviço LTFS reiniciado em {LTFS_DEVICE}.")
                else:
                    _telegram_send(f"⚠️ erase-history concluído mas serviço LTFS falhou ao reiniciar em {LTFS_DEVICE}.")
            else:
                _telegram_send(
                    f"❌ erase-history falhou em {LTFS_DEVICE}.\n"
                    f"Erro: {action_result.get('stderr', '')[:300] or action_result.get('message', '')}"
                )
    elif action == "cursor_recover":
        cursor = _select_recovery_cursor()
        if not cursor or not cursor.get("volser"):
            action_result = {
                "success": False,
                "returncode": 1,
                "message": "Nenhum cursor aberto encontrado para recovery",
                "details": {"open_cursors": []},
            }
        else:
            cr = cursor_recover(str(cursor["volser"]))
            action_result = {
                "success": cr.get("success", False),
                "returncode": 0 if cr.get("success") else 1,
                "message": cr.get("message", ""),
                "stdout": cr.get("message", ""),
                "stderr": "",
                "details": cr.get("details", {}),
                "cursor": cursor,
                "cursor_recover": cr,
            }
    else:
        return {
            "success": False,
            "returncode": 1,
            "message": f"Ação de recovery não suportada: {action}",
            "details": {},
        }

    # Não registra cooldown quando ltfsck foi bloqueado (device ocupado) — evita
    # false-positive que impede nova tentativa após o serviço liberar o device.
    skip_cooldown = action == "ltfsck" and _ltfsck_was_blocked(action_result)
    # erase_history_telegram sem resposta/recusa não consome o slot de cooldown
    if action == "erase_history_telegram":
        reason = action_result.get("details", {}).get("reason", "")
        if reason in ("telegram_timeout", "operator_rejected"):
            skip_cooldown = True
    if not skip_cooldown:
        _record_action_attempt(
            action,
            _recovery_action_succeeded(action_result),
            {
                "stdout": action_result.get("stdout", ""),
                "stderr": action_result.get("stderr", ""),
            },
        )
    if resume_background_units:
        action_result["resume_background_units"] = True
    return action_result


def _choose_escalation_action(
    previous_action: str,
    diagnosis: Dict[str, Any],
    action_result: Dict[str, Any] | None = None,
) -> str | None:
    issue = diagnosis.get("details", {}).get("issue") or {}
    suggested_action = issue.get("recovery_action")

    if previous_action == "selfheal_remount" and suggested_action in {"ltfsck", "deep_recovery"}:
        return suggested_action

    if previous_action == "ltfsck":
        # Qualquer falha do ltfsck (EOD missing, XML inválido, posição inconsistente)
        # → tentar deep_recovery antes de erase-history; deep_recovery faz scan físico
        # e pode reconstruir o índice mesmo com posição errada.
        return "deep_recovery"

    # deep_recovery falhou → único caminho restante é erase-history via Telegram
    if previous_action == "deep_recovery":
        if action_result and not _recovery_action_succeeded(action_result):
            return "erase_history_telegram"

    return None


def _active_unmount_block_units() -> list[Dict[str, Any]]:
    """Retorna writers que tornam stop/unmount inseguro."""
    active_units: list[Dict[str, Any]] = []
    for unit in LTFS_UNMOUNT_BLOCK_UNITS:
        proc = _run_command(["systemctl", "is-active", unit])
        state = ((proc.stdout or proc.stderr).strip() or "inactive").splitlines()[0]
        if state in {"active", "activating", "reloading", "deactivating"}:
            active_units.append({"unit": unit, "active_state": state, "returncode": proc.returncode})
    return active_units


def _unmount_safety_preflight() -> Dict[str, Any]:
    """Bloqueia unmount se houver escrita em andamento ou cursor aberto."""
    mount = _run_command(["findmnt", str(LTFS_MOUNT_POINT)])
    mounted = mount.returncode == 0
    active_units = [] if LTFS_UNMOUNT_ALLOW_ACTIVE_WRITERS else _active_unmount_block_units()
    open_cursors = [] if LTFS_UNMOUNT_ALLOW_OPEN_CURSOR else _list_recovery_cursors()
    blocked = bool(active_units or open_cursors)
    return {
        "success": not blocked,
        "message": (
            "unmount seguro"
            if not blocked
            else "unmount bloqueado: writer ativo ou cursor aberto no device atual"
        ),
        "details": {
            "mountpoint": str(LTFS_MOUNT_POINT),
            "mounted": mounted,
            "active_block_units": active_units,
            "open_cursors": open_cursors,
            "policy": {
                "allow_active_writers": LTFS_UNMOUNT_ALLOW_ACTIVE_WRITERS,
                "allow_open_cursor": LTFS_UNMOUNT_ALLOW_OPEN_CURSOR,
                "block_units": LTFS_UNMOUNT_BLOCK_UNITS,
            },
        },
    }


def orchestrated_mount() -> Dict[str, Any]:
    """Monta LTFS via ltfs-fc-stable-start.

    NÃO usa _run_exclusive_operation: o próprio ltfs-fc-stable-start
    adquire flock exclusivo em LTFS_ORCH_LOCK. Envolver com
    _run_exclusive_operation causa deadlock — Python segura o flock
    enquanto o script filho tenta adquirir o mesmo arquivo via novo fd.
    Lição aprendida: 2026-05-18, sg1/sg2 mount timeout.

    Após mount bem-sucedido abre automaticamente um cursor de escrita
    (checkpoint de sessão) para habilitar cursor_recover em caso de falha.
    O cursor é escrito diretamente — sem _respond extra — para não
    poluir o JSON de saída do chamador.
    """
    LOGGER.info("Iniciando operação exclusiva LTFS: mount")
    service_actions = _stop_conflicting_services()
    _fc_start = os.environ.get("LTFS_FC_STABLE_START", "/var/db/ltfs-tools/ltfs-fc-stable-start")
    result = _run_orchestration_command([_fc_start], streaming=True)
    success = result["returncode"] == 0

    # Primeira recuperacao automatica: EOD ausente -> tenta montar com ultima posicao valida
    auto_ro_result: Dict[str, Any] = {}
    if not success and _is_eod_missing_in_mount_log():
        LOGGER.info("EOD ausente detectado no log de mount -- tentando force_mount_no_eod como primeira recuperacao")
        auto_ro_result = force_mount_ro()
        LOGGER.info("force_mount_no_eod: success=%s", auto_ro_result.get("success"))

    resume_info: Dict[str, Any] = {}
    if success:
        resume_info = {"resumed_background_units": _resume_suspended_units(service_actions)}
    else:
        resume_info = {
            "background_units_paused": True,
            "explanation": "Mount falhou; timers/servicos de escrita ficam pausados para evitar remount/flush concorrente.",
        }
    cursor_info: Dict[str, Any] = {}
    if success:
        volser = _detect_volser()
        LTFS_CURSOR_DIR.mkdir(parents=True, exist_ok=True)
        sid = datetime.now().strftime("%Y%m%d_%H%M%S")
        block = _read_tape_block()
        now_iso = datetime.now().isoformat()
        cursor_data: Dict[str, Any] = {
            "volser": volser,
            "session_id": sid,
            "device": LTFS_DEVICE,
            "tape_device": LTFS_TAPE_DEVICE,
            "opened_at": now_iso,
            "updated_at": now_iso,
            "start_block": block,
            "last_block": block,
            "last_file": None,
            "files_written": [],
            "files_pending": [],
            "status": "in_progress",
        }
        _cursor_write(_cursor_path(volser), cursor_data)
        cursor_info = {"volser": volser, "session_id": sid, "start_block": block}
        LOGGER.info("Cursor de escrita aberto: volser=%s session=%s block=%s", volser, sid, block)
    return _respond(
        success,
        "Operação 'mount' executada",
        {"operation": "mount", "command_result": result, "cursor": cursor_info, "auto_ro_recovery": auto_ro_result, **service_actions, **resume_info},
    )


def orchestrated_stop() -> Dict[str, Any]:
    """Desmonta LTFS de forma orquestrada e exclusiva."""
    # Durante stop, o processo ltfs montado e o holder esperado do device.
    # O wrapper de stop faz o unmount gracioso e aguarda a liberacao; bloquear
    # esse holder aqui faria o systemd partir para terminacao forcada.
    _stop_script = os.environ.get("LTFS_STOP_SCRIPT", "/var/db/ltfs-tools/ltfs-lto6-stop")
    return _run_exclusive_operation(
        "stop",
        [_stop_script],
        extra_allowed_cmd_tokens=("ltfs",),
        preflight=_unmount_safety_preflight,
    )


def deep_recovery() -> Dict[str, Any]:
    """Executa ltfsck --deep-recovery com lock exclusivo de fita.

    Para o loop de Restart= do serviço LTFS antes de adquirir o lock — sem isso,
    o ltfs process racing pode segurar o device enquanto ltfsck tenta iniciar.
    """
    _stop_ltfs_service_loop(wait_seconds=30)
    return _run_exclusive_operation("deep-recovery", [LTFSCK_BIN, "--deep-recovery", LTFS_DEVICE], streaming=True, success_codes=frozenset({0, 1}))


def force_mount_ro() -> Dict[str, Any]:
    """Monta LTFS em RO ignorando EOD ausente — último recurso antes de erase-history/mkltfs.

    Usa force_mount_no_eod do ltfs-patched para salvar dados legíveis mesmo com
    EOD corrompido. Monta em LTFS_RO_RECOVERY_MOUNT (não afeta o mountpoint normal).
    Não inicia o serviço — é apenas uma montagem de salvamento manual.
    """
    LTFS_RO_RECOVERY_MOUNT.mkdir(parents=True, exist_ok=True)
    return _run_exclusive_operation(
        "force-mount-ro",
        [LTFS_BIN, "-o", f"devname={LTFS_DEVICE}", "-o", "force_mount_no_eod", str(LTFS_RO_RECOVERY_MOUNT)],
        streaming=True,
    )


def erase_history() -> Dict[str, Any]:
    """Executa ltfsck --erase-history: reconstrói índice descartando histórico de gerações.

    Usado quando deep-recovery falha por LOCATE -20301. Pode perder dados gravados
    após o último sync periódico (padrão: 5 min). Exige confirmação explícita do operador.
    """
    _stop_ltfs_service_loop(wait_seconds=30)
    return _run_exclusive_operation(
        "erase-history",
        [LTFSCK_BIN, "--erase-history", LTFS_DEVICE],
        streaming=True,
        success_codes=frozenset({0, 1}),
    )


def _cursor_rollback_to_persistence(volser: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Restaura a fita para o rollback point LTFS mais próximo do cursor."""
    cursor_time = _parse_dt(data.get("updated_at", "") or data.get("opened_at", ""))
    list_result = _run_exclusive_operation(
        "ltfsck-list-rollback-points",
        [LTFSCK_BIN, "-l", "-m", LTFS_DEVICE],
        streaming=True,
    )
    command_result = list_result.get("details", {}).get("command_result", {})
    list_output = "\n".join([command_result.get("stdout", ""), command_result.get("stderr", "")])
    points = _parse_ltfsck_rollback_points(list_output)
    selected = _choose_rollback_point(points, cursor_time)
    if not selected:
        return {
            "success": False,
            "message": "Nenhum rollback point LTFS encontrado para restaurar pelo cursor",
            "details": {
                "volser": volser,
                "cursor_time": cursor_time.isoformat() if cursor_time else None,
                "list_result": list_result,
                "rollback_points": points,
            },
        }

    generation = int(selected["generation"])
    rollback_result = _run_exclusive_operation(
        "ltfsck-cursor-rollback",
        [LTFSCK_BIN, "-g", str(generation), "-r", "-j", LTFS_DEVICE],
        streaming=True,
    )

    now = datetime.now().isoformat()
    data["status"] = "rolled_back" if rollback_result["success"] else "rollback_failed"
    data["rolled_back_at"] = now
    data["rollback_generation"] = generation
    data["rollback_cursor_time"] = cursor_time.isoformat() if cursor_time else None
    data["rollback_point"] = {
        "generation": generation,
        "timestamp": selected["timestamp"].isoformat() if selected.get("timestamp") else None,
        "raw": selected.get("raw", []),
    }
    _cursor_write(_cursor_path(volser), data)

    return {
        "success": rollback_result["success"],
        "message": f"Rollback LTFS para geração {generation}",
        "details": {
            "volser": volser,
            "cursor_time": cursor_time.isoformat() if cursor_time else None,
            "selected_point": data["rollback_point"],
            "rollback_points_count": len(points),
            "list_result": list_result,
            "rollback_result": rollback_result,
        },
    }


def self_heal(now: datetime | None = None) -> Dict[str, Any]:
    initial_check = check_catalog(now=now)
    if initial_check["success"]:
        runtime_state = _collect_runtime_state(now=now)
        if not _should_intervene_outside_window(runtime_state) and not _cursor_recovery_issue(runtime_state):
            return _respond(True, "LTFS já está saudável; sem ação corretiva", {"initial_check": initial_check, "runtime_state": runtime_state})

    diagnosis = diagnose_known_issue(now=now)
    diagnosis_issue = diagnosis.get("details", {}).get("issue")
    if not diagnosis.get("success") or not diagnosis_issue:
        return _respond(
            False,
            "Falha LTFS sem assinatura conhecida; escalonar com análise adicional",
            {"initial_check": initial_check, "diagnosis": diagnosis},
        )

    action = diagnosis_issue["recovery_action"]
    if action not in {"selfheal_remount", "ltfsck", "deep_recovery", "cursor_recover"}:
        return _respond(
            False,
            f"Incidente conhecido detectado, mas exige ajuste manual: {diagnosis_issue['title']}",
            {"initial_check": initial_check, "diagnosis": diagnosis},
        )

    cooldown_info = _action_in_cooldown(action, now=now)
    if cooldown_info:
        return _respond(
            False,
            f"Self-heal em cooldown para ação {action}",
            {
                "initial_check": initial_check,
                "diagnosis": diagnosis,
                "cooldown": cooldown_info,
            },
        )

    _telegram_send(
        f"🔧 Self-heal LTFS iniciado em {LTFS_DEVICE}\n"
        f"Problema: {diagnosis_issue['title']}\n"
        f"Ação: {action}"
    )

    recovery_chain: list[Dict[str, Any]] = []
    current_action: str = action
    current_result: Dict[str, Any] | None = None
    final_check: Dict[str, Any] = {}
    followup_diagnosis: Dict[str, Any] | None = None
    MAX_ESCALATION_STEPS = 4

    for _step in range(MAX_ESCALATION_STEPS):
        current_result = _execute_recovery_action(current_action)
        recovery_chain.append({"action": current_action, "result": current_result})

        final_check = check_catalog(now=now)
        if final_check["success"]:
            break

        followup_diagnosis = diagnose_known_issue(now=now)
        next_action = _choose_escalation_action(current_action, followup_diagnosis, current_result)
        if not next_action:
            break

        cooldown_info = _action_in_cooldown(next_action, now=now)
        if cooldown_info:
            details = {
                "initial_check": initial_check,
                "diagnosis": diagnosis,
                "recovery_chain": recovery_chain,
                "final_check": final_check,
                "followup_diagnosis": followup_diagnosis,
                "cooldown": cooldown_info,
            }
            return _respond(False, f"Self-heal em cooldown para ação escalada {next_action}", details)

        current_action = next_action

    # Retoma units de background se o último passo foi bem-sucedido
    last_result = recovery_chain[-1]["result"] if recovery_chain else {}
    if last_result.get("resume_background_units") and final_check.get("success"):
        last_result["background_units_resumed"] = _resume_suspended_unit_sets(
            last_result.get("paused_units"),
            last_result.get("exclusive_paused_units"),
            last_result.get("details"),
        )

    details = {
        "initial_check": initial_check,
        "diagnosis": diagnosis,
        "action_result": recovery_chain[0]["result"] if recovery_chain else {},
        "recovery_chain": recovery_chain,
        "final_check": final_check,
    }
    if followup_diagnosis is not None:
        details["followup_diagnosis"] = followup_diagnosis

    latest_action_result = recovery_chain[-1]["result"] if recovery_chain else {}
    if final_check.get("success") and _recovery_action_succeeded(latest_action_result):
        _telegram_send(f"✅ Self-heal LTFS concluído: {diagnosis_issue['title']}")
        return _respond(True, f"Self-heal LTFS concluído: {diagnosis_issue['title']}", details)

    last_action = recovery_chain[-1]["action"] if recovery_chain else action
    _telegram_send(
        f"❌ Self-heal LTFS não recuperou o serviço: {diagnosis_issue['title']}\n"
        f"Última ação tentada: {last_action}"
    )
    return _respond(
        False,
        f"Self-heal LTFS não recuperou o serviço: {diagnosis_issue['title']}",
        details,
    )


def _parse_window_time(raw_value: str) -> time | None:
    try:
        parsed = datetime.strptime(raw_value, "%H:%M")
    except ValueError:
        return None
    return parsed.time()


def _is_mount_expected(now: datetime | None = None) -> bool:
    if not LTFS_ALLOW_UNMOUNTED_OUTSIDE_WINDOW:
        return True

    start_time = _parse_window_time(LTFS_USAGE_WINDOW_START)
    end_time = _parse_window_time(LTFS_USAGE_WINDOW_END)
    if start_time is None or end_time is None:
        return True

    current = (now or datetime.now()).time()
    if start_time <= end_time:
        return start_time <= current < end_time
    return current >= start_time or current < end_time


def _expected_unmounted_response(now: datetime | None = None) -> Dict[str, Any]:
    open_cursors = _list_recovery_cursors()
    if open_cursors:
        return _respond(
            False,
            "LTFS desmontado fora da janela com cursor aberto; recovery por cursor necessário",
            {
                "mount_expected": False,
                "usage_window_start": LTFS_USAGE_WINDOW_START,
                "usage_window_end": LTFS_USAGE_WINDOW_END,
                "checked_at": (now or datetime.now()).isoformat(),
                "cursor_recovery_required": True,
                "open_cursors": open_cursors,
            },
        )
    return _respond(
        True,
        "LTFS desmontado fora da janela de utilização",
        {
            "mount_expected": False,
            "usage_window_start": LTFS_USAGE_WINDOW_START,
            "usage_window_end": LTFS_USAGE_WINDOW_END,
            "checked_at": (now or datetime.now()).isoformat(),
        },
    )


def check_catalog(now: datetime | None = None) -> Dict[str, Any]:
    if not LTFS_MOUNT_POINT.exists():
        if not _is_mount_expected(now):
            return _expected_unmounted_response(now)
        return _respond(False, f"Mountpoint ausente: {LTFS_MOUNT_POINT}")

    mount = _run_command(["mountpoint", "-q", str(LTFS_MOUNT_POINT)])
    if mount.returncode != 0:
        if not _is_mount_expected(now):
            return _expected_unmounted_response(now)
        return _respond(False, "Mountpoint LTFS inativo", {"stderr": mount.stderr.strip()})

    catalog = _run_command(["ltfs-catalog", "list"])
    if catalog.returncode != 0:
        return _respond(False, "ltfs-catalog list falhou", {"stderr": catalog.stderr.strip()})

    df = _run_command(["df", "-h", str(LTFS_MOUNT_POINT)])
    return _respond(True, "Catálogo LTFS acessível", {"df": df.stdout.strip()})


def _latest_backup_dir() -> Path | None:
    if not BACKUP_ROOT.exists():
        return None
    dirs = [p for p in BACKUP_ROOT.iterdir() if p.is_dir()]
    if not dirs:
        return None
    return max(dirs, key=lambda d: d.stat().st_mtime)


def catalog_restore() -> Dict[str, Any]:
    if not CATALOG_DB:
        return _respond(False, "TAPE_CATALOG_DB não configurado")

    backup_dir = _latest_backup_dir()
    if not backup_dir:
        return _respond(False, "Nenhum backup de catálogo disponível")

    dump_file = backup_dir / "catalog_dump.sql"
    if not dump_file.exists():
        return _respond(False, "Dump do catálogo não encontrado", {"backup_dir": str(backup_dir)})

    restore = _run_command(["psql", CATALOG_DB, "-f", str(dump_file)])
    if restore.returncode != 0:
        return _respond(False, "Restauração do catálogo falhou", {"stderr": restore.stderr.strip()})

    return check_catalog()


def drive_check(now: datetime | None = None) -> Dict[str, Any]:
    catalog_resp = check_catalog(now=now)
    if not catalog_resp["success"]:
        diagnosis = diagnose_known_issue(now=now)
        return _respond(False, "Drive necessita intervenção", {"catalog": catalog_resp, "diagnosis": diagnosis})

    if not catalog_resp["details"].get("mount_expected", True):
        runtime_state = _collect_runtime_state(now=now)
        cursor_issue = _cursor_recovery_issue(runtime_state)
        if cursor_issue:
            return _respond(
                False,
                "Drive fora da janela, mas cursor aberto exige recovery",
                {"catalog": catalog_resp, "runtime_state": runtime_state, "issue": cursor_issue},
            )
        return _respond(True, "Drive LTFS em estado seguro fora da janela", {"catalog": catalog_resp})

    dmesg = _run_command(["dmesg", "-T"])
    warnings = [
        line
        for line in dmesg.stdout.splitlines()
        if (Path(LTFS_TAPE_DEVICE).name in line.lower() or "lto" in line.lower()) and "error" in line.lower()
    ]
    if warnings:
        return _respond(True, "Drive reportou avisos importantes", {"warnings": warnings[:5]})

    return _respond(True, "Drive LTFS saudável")


def backup_catalog() -> Dict[str, Any]:
    if not CATALOG_DB:
        return _respond(False, "TAPE_CATALOG_DB indefinido")

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_ROOT / timestamp
    dest.mkdir(parents=True)

    dump_file = dest / "catalog_dump.sql"
    export_file = dest / "ltfs_catalog_export.json"
    list_file = dest / "ltfs_catalog_list.txt"

    dump = _run_command(["pg_dump", CATALOG_DB, "--no-owner", "-f", str(dump_file)])
    if dump.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        return _respond(False, "pg_dump falhou", {"stderr": dump.stderr.strip()})

    # Tenta exportar o catálogo. Nem todas as versões do ltfs-catalog
    # implementam o subcomando `export`; nesse caso, usa o fallback `list`.
    export = _run_command(["ltfs-catalog", "export", "--file", str(export_file)])
    export_succeeded = export.returncode == 0
    if export_succeeded:
        # Se o comando escreveu no stdout em vez do arquivo, salvamos o conteúdo.
        try:
            if export_file.exists():
                pass
            elif export.stdout:
                export_file.write_text(export.stdout)
        except Exception:
            # Não falhar o backup por erro de escrita auxiliar; deixamos sem export_file.
            export_succeeded = False

    if not export_succeeded:
        # Algumas instalacoes do ltfs-catalog expõem apenas index/query/list.
        list_cmd = _run_command(["ltfs-catalog", "list"])
        if list_cmd.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            return _respond(
                False,
                "ltfs-catalog export falhou",
                {"stderr": export.stderr.strip(), "fallback_stderr": list_cmd.stderr.strip()},
            )
        list_file.write_text(list_cmd.stdout)

    cutoff = datetime.now() - timedelta(days=RETENTION_DAYS)
    cleaned = []
    for child in BACKUP_ROOT.iterdir():
        if not child.is_dir():
            continue
        if datetime.fromtimestamp(child.stat().st_mtime) < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            cleaned.append(child.name)

    details: Dict[str, Any] = {"dest": str(dest), "cleaned": cleaned}
    if export_file.exists():
        details["export_file"] = str(export_file)
    if list_file.exists():
        details["list_file"] = str(list_file)

    return _respond(True, "Backup concluído", details)


# ─── Write Cursor — checkpoint de sessão de escrita ───────────────────────────

def _cursor_path(volser: str) -> Path:
    return LTFS_CURSOR_DIR / f"{volser}.json"


def _read_tape_block() -> int | None:
    """Lê posição atual do bloco na fita via mt tell (nst device)."""
    proc = _run_command(["mt", "-f", LTFS_TAPE_DEVICE, "tell"])
    for line in (proc.stdout or "").splitlines():
        line_l = line.lower()
        if "block" in line_l:
            for token in line.split():
                token_clean = token.rstrip(".")
                if token_clean.isdigit():
                    return int(token_clean)
    return None


def _cursor_write(path: Path, data: Dict[str, Any]) -> None:
    """Escrita atômica do cursor via rename."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.rename(path)


def _cursor_read(volser: str) -> tuple[Dict[str, Any] | None, str]:
    """Lê cursor; retorna (dados, erro). erro='' se ok."""
    path = _cursor_path(volser)
    if not path.exists():
        return None, f"Cursor não encontrado: {path}"
    try:
        return json.loads(path.read_text()), ""
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Erro ao ler cursor: {exc}"


def _cursor_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    """Resumo seguro para respostas CLI sem despejar listas grandes no journal."""
    files_written = data.get("files_written") or []
    files_pending = data.get("files_pending") or []
    return {
        "volser": data.get("volser"),
        "session_id": data.get("session_id"),
        "device": data.get("device"),
        "tape_device": data.get("tape_device"),
        "opened_at": data.get("opened_at"),
        "updated_at": data.get("updated_at"),
        "closed_at": data.get("closed_at"),
        "status": data.get("status"),
        "start_block": data.get("start_block"),
        "last_block": data.get("last_block"),
        "final_block": data.get("final_block"),
        "last_file": data.get("last_file"),
        "files_written_count": len(files_written),
        "files_pending_count": len(files_pending),
    }


def _cursor_needs_recovery(data: Dict[str, Any]) -> bool:
    return str(data.get("status") or "").strip() in LTFS_CURSOR_RECOVERY_STATUSES


def _cursor_has_write_progress(data: Dict[str, Any]) -> bool:
    return bool(data.get("last_file") or data.get("files_written") or data.get("files_pending"))


def _list_recovery_cursors() -> list[Dict[str, Any]]:
    """Lista cursores abertos que tornam um unmount suspeito."""
    if not LTFS_CURSOR_DIR.exists():
        return []
    cursors: list[Dict[str, Any]] = []
    for path in sorted(LTFS_CURSOR_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not _cursor_needs_recovery(data):
            continue
        if not _cursor_has_write_progress(data):
            continue
        cursor_device = data.get("device")
        cursor_tape_device = data.get("tape_device")
        if cursor_device and cursor_device != LTFS_DEVICE:
            continue
        if cursor_tape_device and cursor_tape_device != LTFS_TAPE_DEVICE:
            continue
        summary = _cursor_summary(data)
        summary["cursor_file"] = str(path)
        cursors.append(summary)
    return cursors


def _select_recovery_cursor(cursors: list[Dict[str, Any]] | None = None) -> Dict[str, Any] | None:
    candidates = cursors if cursors is not None else _list_recovery_cursors()
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda cursor: (
            _parse_dt(str(cursor.get("updated_at", ""))) or datetime.min,
            str(cursor.get("volser") or ""),
        ),
    )


def cursor_open(volser: str, session_id: str | None = None) -> Dict[str, Any]:
    """
    Abre uma sessão de escrita na fita e registra o bloco inicial.
    Deve ser chamado ANTES de qualquer write na sessão.
    """
    LTFS_CURSOR_DIR.mkdir(parents=True, exist_ok=True)
    sid = session_id or datetime.now().strftime("%Y%m%d_%H%M%S")
    block = _read_tape_block()
    now = datetime.now().isoformat()
    cursor: Dict[str, Any] = {
        "volser": volser,
        "session_id": sid,
        "device": LTFS_DEVICE,
        "tape_device": LTFS_TAPE_DEVICE,
        "opened_at": now,
        "updated_at": now,
        "start_block": block,
        "last_block": block,
        "last_file": None,
        "files_written": [],
        "files_pending": [],
        "status": "in_progress",
    }
    _cursor_write(_cursor_path(volser), cursor)
    return _respond(True, f"Cursor aberto: sessão {sid} a partir do bloco {block}", {
        "cursor": cursor,
        "cursor_file": str(_cursor_path(volser)),
    })


def cursor_update(volser: str, file_path: str, block: int | None = None) -> Dict[str, Any]:
    """
    Atualiza o cursor após gravar um arquivo com sucesso.
    Se block não for passado, lê a posição atual via mt tell.
    """
    data, err = _cursor_read(volser)
    if err:
        return _respond(False, err, {"volser": volser})
    now = datetime.now().isoformat()
    if data.get("status") == "clean":
        data["status"] = "in_progress"
        data["reopened_at"] = now
        data.pop("closed_at", None)
        data.pop("final_block", None)
    current_block = block if block is not None else _read_tape_block()
    data["last_block"] = current_block
    data["last_file"] = file_path
    data["updated_at"] = now
    data["files_written"].append({
        "path": file_path,
        "block": current_block,
        "written_at": now,
    })
    _cursor_write(_cursor_path(volser), data)
    return _respond(True, f"Cursor atualizado: bloco {current_block} — {file_path}", {
        "cursor_file": str(_cursor_path(volser)),
        "last_block": current_block,
        "files_written_count": len(data["files_written"]),
    })


def cursor_close(volser: str) -> Dict[str, Any]:
    """
    Encerra a sessão de escrita com status 'clean'.
    Indica que a fita está consistente e não precisa de recovery.
    """
    data, err = _cursor_read(volser)
    if err:
        return _respond(False, err, {"volser": volser})
    block = _read_tape_block()
    now = datetime.now().isoformat()
    data["status"] = "clean"
    data["closed_at"] = now
    data["final_block"] = block
    _cursor_write(_cursor_path(volser), data)
    return _respond(True, f"Sessão encerrada limpa: {data['session_id']} — bloco final {block}", {
        "cursor": _cursor_summary(data),
        "cursor_file": str(_cursor_path(volser)),
        "files_written_count": len(data["files_written"]),
    })


def cursor_status(volser: str) -> Dict[str, Any]:
    """Exibe estado atual do cursor de escrita para um volser."""
    data, err = _cursor_read(volser)
    if err:
        return _respond(False, err, {"volser": volser})
    return _respond(True, f"Cursor {volser}: {data.get('status')} — bloco {data.get('last_block')}", {
        "cursor": _cursor_summary(data),
    })


def cursor_recover(volser: str) -> Dict[str, Any]:
    """
    Recovery a partir do cursor de escrita:
      1. Lê o checkpoint salvo (last_block + arquivos confirmados)
      2. Pausa units auxiliares
      3. Executa ltfsck para reconstruir o índice LTFS
      4. Reinicia o serviço LTFS
      5. Reporta arquivos confirmados e pendentes (para re-fila)

    Analogia: download manager que retoma do byte onde parou.
    Os arquivos em files_written foram confirmados ANTES da falha.
    Os arquivos em files_pending estavam "em voo" — precisam ser re-escritos.
    """
    data, err = _cursor_read(volser)
    if err:
        return _respond(False, err, {"volser": volser})

    if data.get("status") == "clean":
        return _respond(True, f"Cursor {volser} já está limpo — nenhum recovery necessário", {"cursor": data})

    last_block = data.get("last_block")
    files_written = data.get("files_written", [])
    files_pending = data.get("files_pending", [])
    files_recovered = files_written
    files_to_requeue = files_pending

    LOGGER.info("cursor_recover: volser=%s last_block=%s files_confirmed=%d", volser, last_block, len(files_written))

    paused = _pause_background_ltfs_units()

    ltfsck_result = _run_exclusive_operation(
        "ltfsck-cursor-recover",
        [LTFSCK_BIN, "-f", LTFS_DEVICE],
        streaming=True,
    )
    rollback_result: Dict[str, Any] | None = None

    # ltfsck -f não resolve EOD missing — escalar automaticamente para --deep-recovery
    if not ltfsck_result["success"] and _ltfsck_needs_deep_recovery(ltfsck_result):
        LOGGER.warning(
            "ltfsck -f insuficiente (EOD missing detectado) — escalando para --deep-recovery em %s",
            LTFS_DEVICE,
        )
        data["ltfsck_basic_rc"] = ltfsck_result.get("details", {}).get("command_result", {}).get("returncode", -1)
        ltfsck_result = _run_exclusive_operation(
            "ltfsck-cursor-deep-recover",
            [LTFSCK_BIN, "--deep-recovery", LTFS_DEVICE],
            streaming=True,
        )

    if not ltfsck_result["success"]:
        LOGGER.warning("ltfsck não recuperou o cursor — tentando rollback para último índice persistido")
        rollback_result = _cursor_rollback_to_persistence(volser, data)
        if rollback_result.get("success"):
            selected_point = rollback_result.get("details", {}).get("selected_point", {})
            rollback_point = {
                "generation": selected_point.get("generation"),
                "timestamp": _parse_dt(selected_point.get("timestamp", "") or ""),
                "raw": selected_point.get("raw", []),
            }
            files_recovered, files_to_requeue = _split_files_by_rollback_point(
                files_written,
                files_pending,
                rollback_point,
            )
            data["files_written_before_rollback"] = files_written
            data["files_written"] = files_recovered
            data["files_pending"] = files_to_requeue
            ltfsck_result = rollback_result.get("details", {}).get("rollback_result", rollback_result)

    now = datetime.now().isoformat()
    if rollback_result and rollback_result.get("success"):
        data["status"] = "rolled_back"
    else:
        data["status"] = "recovered" if ltfsck_result["success"] else "recover_failed"
    data["recovered_at"] = now
    data["recovered_block"] = last_block
    data["ltfsck_rc"] = ltfsck_result.get("details", {}).get("command_result", {}).get("returncode", -1)
    _cursor_write(_cursor_path(volser), data)

    restart_success = False
    if ltfsck_result["success"]:
        reset = _run_command(["systemctl", "reset-failed", LTFS_SERVICE])
        start = _run_command(["systemctl", "start", LTFS_SERVICE])
        resume_result: Dict[str, Any] | None = None
        if start.returncode == 0:
            restart_success = True
            resume_result = _resume_suspended_unit_sets(
                paused,
                ltfsck_result.get("details"),
                (rollback_result or {}).get("details", {}).get("list_result", {}).get("details"),
                (rollback_result or {}).get("details", {}).get("rollback_result", {}).get("details"),
            )
    else:
        reset = None
        start = None
        resume_result = None
    overall_success = ltfsck_result["success"] and restart_success

    return _respond(
        overall_success,
        f"Recovery do cursor {volser}: {len(files_recovered)} arquivos recuperados, {len(files_to_requeue)} para re-fila",
        {
            "volser": volser,
            "last_block": last_block,
            "files_recovered": files_recovered,
            "files_to_requeue": files_to_requeue,
            "ltfsck_result": ltfsck_result,
            "rollback_result": rollback_result,
            "paused_units": paused,
            "service_restart_success": restart_success,
            "post_restart": {
                "reset_failed": {
                    "returncode": reset.returncode,
                    "stdout": (reset.stdout or "").strip(),
                    "stderr": (reset.stderr or "").strip(),
                } if reset is not None else None,
                "start": {
                    "returncode": start.returncode,
                    "stdout": (start.stdout or "").strip(),
                    "stderr": (start.stderr or "").strip(),
                } if start is not None else None,
            },
            "background_units_resumed": resume_result,
            "cursor_file": str(_cursor_path(volser)),
        },
    )


def cursor_list() -> Dict[str, Any]:
    """Lista todos os cursores ativos no LTFS_CURSOR_DIR."""
    if not LTFS_CURSOR_DIR.exists():
        return _respond(True, "Nenhum cursor encontrado (diretório ausente)", {"cursors": []})
    cursors = []
    for p in sorted(LTFS_CURSOR_DIR.glob("*.json")):
        try:
            c = json.loads(p.read_text())
            cursors.append({
                "volser": c.get("volser"),
                "status": c.get("status"),
                "session_id": c.get("session_id"),
                "last_block": c.get("last_block"),
                "updated_at": c.get("updated_at"),
                "files_written_count": len(c.get("files_written", [])),
                "files_pending_count": len(c.get("files_pending", [])),
            })
        except (OSError, json.JSONDecodeError):
            cursors.append({"file": p.name, "error": "leitura falhou"})
    return _respond(True, f"{len(cursors)} cursor(es) encontrado(s)", {"cursors": cursors})


def prepare_mirror() -> Dict[str, Any]:
    return _respond(
        True,
        "Fita secundária aguardando chegada",
        {"instructions": "Registre a nova fita no catálogo e reexecute python3 /usr/local/tools/ltfs_recovery.py --prepare-mirror quando disponível"},
    )


def repair_partition1_label(volser: str = "") -> Dict[str, Any]:
    """Repara o label ANSI corrompido (< 80 bytes) na partição 1, bloco 0.

    QUANDO USAR: após SIGKILL mid-write do processo LTFS/FUSE, o label da
    partição 1 fica truncado (ex: 17 bytes em vez de 80). Todos os caminhos
    do ltfsck falham com 'Cannot read ANSI label: expected 80 bytes'.

    MECANISMO:
    1. Para o loop de restart do serviço LTFS (mask --runtime + stop)
    2. Para serviços conflitantes e adquire LTFS_ORCH_LOCK exclusivo
    3. Verifica que o device está livre
    4. LOCATE(10) com CP=1 para posicionar em partição 1, bloco 0
    5. WRITE(6) em modo variável — escreve label ANSI correto de 80 bytes
    6. O label gravado é: b'VOL1' + volser(6c) + b'L' + 13 espaços + b'LTFS' + 51 espaços + b'4'

    Após sucesso: executar --deep-recovery para reconstruir índice da partição 0.
    """
    if not volser:
        detected = _detect_volser()
        volser = detected if not detected.startswith("UNKNOWN") else os.getenv("LTFS_VOLSER", "SG0001")

    volser_clean = volser.strip()[:6].upper()
    # LTFS ANSI VOL1 label — 80 bytes exatos (formato IBM LTFS, verificado por leitura da p0):
    #   [0-3]   "VOL1"   label identifier
    #   [4-9]   volser   6 chars
    #   [10]    'L'      accessibility ('L' = LTFS volume — validado por LTFS11176E)
    #   [11-23] 13 espaços  reserved
    #   [24-27] "LTFS"   implementation identifier (4 chars, offset 24)
    #   [28-78] 51 espaços  reserved + owner identifier
    #   [79]    '4'      label standard version
    label_bytes = (
        b"VOL1"
        + f"{volser_clean:<6}".encode("ascii")
        + b"L"       # byte 10: accessibility = 'L' (LTFS marker)
        + b" " * 13  # bytes 11-23: reserved
        + b"LTFS"    # bytes 24-27: implementation identifier
        + b" " * 51  # bytes 28-78: reserved + owner
        + b"4"       # byte 79: label standard version
    )
    if len(label_bytes) != 80:
        return _respond(False, f"Label ANSI inválido: {len(label_bytes)} bytes (esperado 80)", {
            "operation": "repair-partition1-label", "volser": volser_clean,
        })

    LOGGER.info("repair-partition1-label: device=%s volser=%s label=%s",
                LTFS_DEVICE, volser_clean, label_bytes.decode("ascii"))

    # 1. Parar o loop de restart antes de qualquer operação no device
    _stop_ltfs_service_loop(wait_seconds=30)

    # 2. Parar conflitantes e adquirir lock exclusivo
    service_actions = _stop_conflicting_services()

    label_file = Path(f"/tmp/ansi_label_{volser_clean}.bin")
    try:
        label_file.write_bytes(label_bytes)

        with _exclusive_tape_lock(wait_seconds=60):
            # 3. Verificar device livre
            holders = _list_tape_holders()
            unexpected = _filter_unexpected_holders(
                holders, allowed_pids={os.getpid(), os.getppid()}
            )
            if unexpected:
                return _respond(False, "Device ainda ocupado após parar serviços", {
                    "operation": "repair-partition1-label",
                    "holders": unexpected,
                    **service_actions,
                })

            # 4. LOCATE(10) CP=1: posicionar em partição 1, bloco 0
            # CDB: opcode=0x2B CP=0x02 reserved LOID(4B)=0 reserved PART=0x01 ctrl=0x00
            locate_cdb = ["0x2B", "0x02", "0x00", "0x00", "0x00", "0x00", "0x00", "0x00", "0x01", "0x00"]
            locate_result = _run_orchestration_command(
                ["sg_raw", "-v", LTFS_DEVICE] + locate_cdb
            )
            LOGGER.info("LOCATE(10) RC=%d stdout=%s stderr=%s",
                        locate_result["returncode"],
                        locate_result.get("stdout", "")[:200],
                        locate_result.get("stderr", "")[:200])

            if locate_result["returncode"] != 0:
                return _respond(False, "LOCATE(10) para partição 1 falhou — device pode não suportar troca de partição por CDB direto", {
                    "operation": "repair-partition1-label",
                    "step": "locate",
                    "volser": volser_clean,
                    "locate_cdb": " ".join(locate_cdb),
                    "locate_result": locate_result,
                    "sugestao": "Verificar se o drive suporta LOCATE(10) com CP=1; tentar mt setp 1 como alternativa",
                    **service_actions,
                })

            # 5. WRITE(6) variável: escrever label ANSI de 80 bytes
            # CDB: opcode=0x0A FIXED=0 TL(3B)=0x000050(=80) ctrl=0x00
            # -s 80: send 80 bytes (data-out); -i FILE: ler dados do arquivo (sg_raw >= 0.4)
            write_cdb = ["0x0A", "0x00", "0x00", "0x00", "0x50", "0x00"]
            write_result = _run_orchestration_command(
                ["sg_raw", "-v", "-s", "80", "-i", str(label_file), LTFS_DEVICE] + write_cdb
            )
            LOGGER.info("WRITE(6) RC=%d stdout=%s stderr=%s",
                        write_result["returncode"],
                        write_result.get("stdout", "")[:200],
                        write_result.get("stderr", "")[:200])

            if write_result["returncode"] != 0:
                return _respond(False, f"WRITE(6) label falhou — volser={volser_clean}", {
                    "operation": "repair-partition1-label",
                    "step": "write-label",
                    "volser": volser_clean,
                    "locate_result": locate_result,
                    "write_result": write_result,
                    **service_actions,
                })

            # 6. WRITE FILEMARKS(6): gravar 1 filemark após o label
            # O erase head apaga o filemark original ao escrever o label; é obrigatório
            # reescrever o filemark para que LTFS11295E não ocorra na próxima leitura.
            # CDB: opcode=0x10 IMMED=0 WSMK=0 COUNT(3B)=0x000001 ctrl=0x00
            wfm_result = _run_orchestration_command(
                ["sg_raw", "-v", LTFS_DEVICE,
                 "0x10", "0x00", "0x00", "0x00", "0x01", "0x00"]
            )
            LOGGER.info("WRITE FILEMARKS(6) RC=%d stdout=%s stderr=%s",
                        wfm_result["returncode"],
                        wfm_result.get("stdout", "")[:200],
                        wfm_result.get("stderr", "")[:200])

            success = wfm_result["returncode"] == 0
            return _respond(
                success,
                f"Repair ANSI label partição 1 {'concluído' if success else 'falhou (filemark)'} — volser={volser_clean}",
                {
                    "operation": "repair-partition1-label",
                    "volser": volser_clean,
                    "label_hex": label_bytes.hex(),
                    "label_ascii": label_bytes.decode("ascii"),
                    "locate_result": locate_result,
                    "write_result": write_result,
                    "filemark_result": wfm_result,
                    "next_step": "--deep-recovery para reconstruir índice da partição 0" if success else None,
                    **service_actions,
                },
            )
    except RuntimeError as exc:
        return _respond(False, str(exc), {
            "operation": "repair-partition1-label",
            "lock_file": str(LTFS_ORCH_LOCK),
        })
    finally:
        label_file.unlink(missing_ok=True)


def repair_partition1_ltfs_label() -> Dict[str, Any]:
    """Restaura o bloco LTFS label XML na partição 1 copiando da partição 0.

    QUANDO USAR: após --repair-partition1-label, o bloco XML (bloco 1 da partição 1)
    foi apagado pelo erase head durante a escrita do VOL1 label + filemark.
    Sem esse bloco, ltfsck falha com LTFS11178E.

    MECANISMO:
    1. LOCATE partição 0 bloco 0 → READ VOL1 (80 bytes) → SPACE 1 FM
    2. READ bloco 1 da partição 0 = LTFS label XML (até 256KB)
    3. LOCATE partição 1 bloco 0 → READ VOL1 → SPACE 1 FM  (posiciona em EOD p1)
    4. Patch XML: <partition>a→b, limpa <location> e <previousgenerationlocation>
    5. WRITE o XML patcheado
    6. WRITE FILEMARKS(6) para terminar

    Após sucesso: executar --erase-history.
    Se --erase-history falhar com LTFS11205E (back pointer em p0 para índice inexistente
    em p1): tentar --rollback-generation0; se persistir, usar --reformat.
    """
    _stop_ltfs_service_loop(wait_seconds=30)
    service_actions = _stop_conflicting_services()

    try:
        with _exclusive_tape_lock(wait_seconds=60):
            holders = _list_tape_holders()
            unexpected = _filter_unexpected_holders(holders, allowed_pids={os.getpid(), os.getppid()})
            if unexpected:
                return _respond(False, "Device ocupado", {
                    "operation": "repair-partition1-ltfs-label",
                    "holders": unexpected,
                    **service_actions,
                })

            def _sg_locate(partition: int, block: int) -> dict:
                cdb = ["0x2B", "0x02",
                       "0x%02x" % ((block >> 24) & 0xff),
                       "0x%02x" % ((block >> 16) & 0xff),
                       "0x%02x" % ((block >> 8)  & 0xff),
                       "0x%02x" % (block & 0xff),
                       "0x00", "0x00",
                       "0x%02x" % partition,
                       "0x00"]
                return _run_orchestration_command(["sg_raw", "-v", LTFS_DEVICE] + cdb)

            def _sg_read(outfile: str, maxbytes: int) -> dict:
                cdb = ["0x08", "0x00",
                       "0x%02x" % ((maxbytes >> 16) & 0xff),
                       "0x%02x" % ((maxbytes >> 8)  & 0xff),
                       "0x%02x" % (maxbytes & 0xff),
                       "0x00"]
                return _run_orchestration_command(
                    ["sg_raw", "-r", str(maxbytes), "-o", outfile, LTFS_DEVICE] + cdb
                )

            def _sg_space_fm(count: int = 1) -> dict:
                cdb = ["0x11", "0x01",
                       "0x%02x" % ((count >> 16) & 0xff),
                       "0x%02x" % ((count >> 8)  & 0xff),
                       "0x%02x" % (count & 0xff),
                       "0x00"]
                return _run_orchestration_command(["sg_raw", "-v", LTFS_DEVICE] + cdb)

            p0_ltfslabel_file = Path("/tmp/p0_ltfslabel.bin")

            # ── Passo 1: ler LTFS label XML da partição 0 ──
            r = _sg_locate(partition=0, block=0)
            if r["returncode"] != 0:
                return _respond(False, "LOCATE p0 bloco 0 falhou", {"step": "locate-p0", "result": r, **service_actions})
            time_module.sleep(0.3)

            r = _sg_read("/tmp/discard_vol1_p0.bin", 80)
            if r["returncode"] != 0:
                return _respond(False, "READ VOL1 p0 falhou", {"step": "read-vol1-p0", "result": r, **service_actions})
            time_module.sleep(0.3)

            r = _sg_space_fm(1)
            if r["returncode"] != 0:
                return _respond(False, "SPACE FM p0 falhou", {"step": "space-fm-p0", "result": r, **service_actions})
            time_module.sleep(0.3)

            # Lê até 256KB — LTFS label XML é tipicamente < 4KB
            r = _sg_read(str(p0_ltfslabel_file), 262144)
            # ILI (block menor que pedido) pode retornar rc != 0 em alguns sg_raw; aceitar se arquivo existe
            ltfslabel_data = b""
            if p0_ltfslabel_file.exists():
                ltfslabel_data = p0_ltfslabel_file.read_bytes()
            if not ltfslabel_data:
                return _respond(False, "READ LTFS label p0 retornou vazio", {
                    "step": "read-ltfslabel-p0", "result": r, **service_actions
                })
            LOGGER.info("LTFS label p0: %d bytes — %s...",
                        len(ltfslabel_data), ltfslabel_data[:120].decode("utf-8", errors="replace"))

            # ── Passo 2: posicionar na partição 1 após o filemark e escrever ──
            r = _sg_locate(partition=1, block=0)
            if r["returncode"] != 0:
                return _respond(False, "LOCATE p1 bloco 0 falhou", {"step": "locate-p1", "result": r, **service_actions})
            time_module.sleep(0.3)

            r = _sg_read("/tmp/discard_vol1_p1.bin", 80)
            if r["returncode"] != 0:
                return _respond(False, "READ VOL1 p1 falhou", {"step": "read-vol1-p1", "result": r, **service_actions})
            time_module.sleep(0.3)

            r = _sg_space_fm(1)
            if r["returncode"] != 0:
                return _respond(False, "SPACE FM p1 falhou", {"step": "space-fm-p1", "result": r, **service_actions})
            time_module.sleep(0.3)

            # Determinar o ID desta partição (p1) a partir do XML da partição 0.
            # O campo <location><partition>X</partition></location> identifica em qual
            # partição o label está gravado. O label da partição 0 diz "a" (index).
            # A partição 1 deve ser a "data" partition, cujo ID vem de <partitions><data>.
            import xml.etree.ElementTree as _ET
            try:
                _tree = _ET.fromstring(ltfslabel_data)
                _p0_id = _tree.findtext("location/partition") or ""
                _index_id = _tree.findtext("partitions/index") or ""
                _data_id  = _tree.findtext("partitions/data") or ""
                # A partição 1 (SCSI) é a outra em relação à p0
                _p1_id = _data_id if _p0_id == _index_id else _index_id
                if not _p1_id:
                    _p1_id = "b" if _p0_id == "a" else "a"
                LOGGER.info("IDs: p0=%r index=%r data=%r → p1 label deve ter location/partition=%r",
                            _p0_id, _index_id, _data_id, _p1_id)
                # Substituição pontual no XML — preserva formatação e tamanho original
                _old = ("<partition>%s</partition>" % _p0_id).encode()
                _new = ("<partition>%s</partition>" % _p1_id).encode()
                if _old not in ltfslabel_data:
                    return _respond(False, "Não encontrou %r no XML para substituir" % _old.decode(), {
                        "step": "patch-xml", **service_actions
                    })
                ltfslabel_p1 = ltfslabel_data.replace(_old, _new, 1)
                LOGGER.info("XML p1 patched: %r → %r", _old.decode(), _new.decode())

                # Limpa back-pointers estáticos para evitar LTFS11205E após a escrita.
                # O XML copiado de p0 tem <location> e <previousgenerationlocation>
                # apontando para blocos de p1 que foram apagados pelo repair-partition1-label.
                # Sem essa limpeza, ltfsck --erase-history falha porque tenta ler esses blocos.
                import re as _re
                _p1_id_b = _p1_id.encode()
                ltfslabel_p1 = _re.sub(
                    rb'<location>.*?</location>',
                    b'<location><partition>' + _p1_id_b + b'</partition><startblock>0</startblock></location>',
                    ltfslabel_p1, flags=_re.DOTALL,
                )
                ltfslabel_p1 = _re.sub(
                    rb'\s*<previousgenerationlocation>.*?</previousgenerationlocation>',
                    b'',
                    ltfslabel_p1, flags=_re.DOTALL,
                )
                LOGGER.info("XML p1 back-pointers limpos: <location>→bloco 0, <previousgenerationlocation> removida")
            except Exception as _e:
                return _respond(False, "Falha ao parsear/patch XML: %s" % _e, {
                    "step": "patch-xml", **service_actions
                })

            # WRITE LTFS label XML (com <location><partition> correto para p1)
            sz = len(ltfslabel_p1)
            write_cdb = ["0x0A", "0x00",
                         "0x%02x" % ((sz >> 16) & 0xff),
                         "0x%02x" % ((sz >> 8)  & 0xff),
                         "0x%02x" % (sz & 0xff),
                         "0x00"]
            p0_ltfslabel_file.write_bytes(ltfslabel_p1)
            r_write = _run_orchestration_command(
                ["sg_raw", "-v", "-s", str(sz), "-i", str(p0_ltfslabel_file), LTFS_DEVICE] + write_cdb
            )
            LOGGER.info("WRITE LTFS label p1 RC=%d stderr=%s",
                        r_write["returncode"], r_write.get("stderr", "")[:200])
            if r_write["returncode"] != 0:
                return _respond(False, "WRITE LTFS label p1 falhou", {
                    "step": "write-ltfslabel-p1", "result": r_write, **service_actions
                })

            # WRITE FILEMARKS para terminar o bloco XML
            r_fm = _run_orchestration_command(
                ["sg_raw", "-v", LTFS_DEVICE, "0x10", "0x00", "0x00", "0x00", "0x01", "0x00"]
            )
            LOGGER.info("WRITE FM p1 RC=%d", r_fm["returncode"])

            success = r_fm["returncode"] == 0
            return _respond(
                success,
                (f"LTFS label partição 1 restaurado ({sz} bytes, location={_p1_id!r}:0) — "
                 "próximo passo: --erase-history (se LTFS11205E → --rollback-generation0 → --reformat)") if success
                else "WRITE FM após LTFS label falhou",
                {
                    "operation": "repair-partition1-ltfs-label",
                    "ltfslabel_size": sz,
                    "p0_partition_id": _p0_id,
                    "p1_partition_id": _p1_id,
                    "ltfslabel_p1_preview": ltfslabel_p1[:300].decode("utf-8", errors="replace"),
                    "write_result": r_write,
                    "fm_result": r_fm,
                    **service_actions,
                },
            )
    except RuntimeError as exc:
        return _respond(False, str(exc), {"operation": "repair-partition1-ltfs-label"})
    finally:
        for f in ("/tmp/discard_vol1_p0.bin", "/tmp/discard_vol1_p1.bin", "/tmp/p0_ltfslabel.bin"):
            Path(f).unlink(missing_ok=True)


def rollback_generation0() -> Dict[str, Any]:
    """ltfsck -r -g 0 -j: rollback para geração 0 (formato inicial) + apaga histórico.

    Cria um volume vazio mas consistente e montável quando a checagem de
    consistência normal falha (ex: back pointer sem alvo na partição de dados).
    Equivalente a um 'format fresh' sem mkltfs — preserva UUID e metadados.
    """
    _stop_ltfs_service_loop(wait_seconds=30)
    return _run_exclusive_operation(
        "rollback-generation0",
        [LTFSCK_BIN, "-r", "-g", "0", "-j", LTFS_DEVICE],
        streaming=True,
        success_codes=frozenset({0, 1}),
    )


def reformat(volser: str) -> Dict[str, Any]:
    """Reformata a fita com mkltfs — apaga TODOS os dados permanentemente.

    Último recurso quando a estrutura LTFS está irrecuperável. Requer --volser
    com o serial de exatamente 6 chars (barcode). O block-size padrão é 524288
    para compatibilidade com o formato original SG0001.
    """
    if not volser:
        return _respond(False, "--reformat requer --volser VOLSER (6 chars, ex: SG0001)")
    if len(volser) != 6:
        return _respond(False, f"--volser deve ter exatamente 6 chars; recebido: {volser!r} ({len(volser)} chars)")
    _stop_ltfs_service_loop(wait_seconds=30)
    return _run_exclusive_operation(
        "reformat",
        [MKLTFS_BIN, "-d", LTFS_DEVICE, "-s", volser, "-n", volser, "-b", "524288", "-f"],
        streaming=True,
    )


def run_mode(mode: str, volser: str = "", file_path: str = "", block: int | None = None, session_id: str | None = None) -> Dict[str, Any]:
    if mode == "check":
        return check_catalog()
    if mode == "diagnose":
        return diagnose_known_issue()
    if mode == "self-heal":
        return self_heal()
    if mode == "catalog-restore":
        return catalog_restore()
    if mode == "drive-check":
        return drive_check()
    if mode == "backup-catalog":
        return backup_catalog()
    if mode == "prepare-mirror":
        return prepare_mirror()
    if mode == "repair-partition1-label":
        return repair_partition1_label(volser=volser)
    if mode == "repair-partition1-ltfs-label":
        return repair_partition1_ltfs_label()
    if mode == "rollback-generation0":
        return rollback_generation0()
    if mode == "reformat":
        if not volser:
            return _respond(False, "--reformat requer --volser VOLSER")
        return reformat(volser=volser)
    if mode == "orchestrated-mount":
        return orchestrated_mount()
    if mode == "orchestrated-stop":
        return orchestrated_stop()
    if mode == "deep-recovery":
        return deep_recovery()
    if mode == "force-mount-ro":
        return force_mount_ro()
    if mode == "erase-history":
        return erase_history()
    # ── cursor ──
    if mode == "cursor-open":
        if not volser:
            return _respond(False, "--cursor-open requer --volser VOLSER")
        return cursor_open(volser, session_id=session_id)
    if mode == "cursor-update":
        if not volser or not file_path:
            return _respond(False, "--cursor-update requer --volser VOLSER --file CAMINHO")
        return cursor_update(volser, file_path, block=block)
    if mode == "cursor-close":
        if not volser:
            return _respond(False, "--cursor-close requer --volser VOLSER")
        return cursor_close(volser)
    if mode == "cursor-status":
        if not volser:
            return _respond(False, "--cursor-status requer --volser VOLSER")
        return cursor_status(volser)
    if mode == "cursor-recover":
        if not volser:
            return _respond(False, "--cursor-recover requer --volser VOLSER")
        return cursor_recover(volser)
    if mode == "cursor-list":
        return cursor_list()
    return _respond(False, f"Modo desconhecido: {mode}")


def main() -> None:
    global DEBUG
    parser = argparse.ArgumentParser(description="LTFS recovery acionado por alertas")
    parser.add_argument(
        "--debug",
        action="store_true",
        default=DEBUG,
        help="Modo debug: streaming em tempo real + logs detalhados (equivale a LTFS_DEBUG=1)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Valida mountpoint e catalogo LTFS")
    group.add_argument("--diagnose", action="store_true", help="Classifica o incidente LTFS por assinatura conhecida")
    group.add_argument("--self-heal", action="store_true", help="Tenta auto-correção para incidentes conhecidos")
    group.add_argument("--catalog-restore", action="store_true", help="Restaura o catalogo a partir do backup mais recente")
    group.add_argument("--drive-check", action="store_true", help="Inspeciona drive e logs do LTFS")
    group.add_argument("--backup-catalog", action="store_true", help="Gera dump diario do catalogo LTFS")
    group.add_argument("--prepare-mirror", action="store_true", help="Registra preparo para futura fita secundaria")
    group.add_argument("--repair-partition1-label", action="store_true",
                       help="Repara label ANSI corrompido (<80 bytes) na partição 1 via LOCATE(10)+WRITE(6); requer --volser se não detectável")
    group.add_argument("--repair-partition1-ltfs-label", action="store_true",
                       help="Restaura bloco LTFS label XML na partição 1 copiando da partição 0 (usar após --repair-partition1-label)")
    group.add_argument("--orchestrated-mount", action="store_true", help="Monta LTFS com lock exclusivo e bloqueio de concorrentes")
    group.add_argument("--orchestrated-stop", action="store_true", help="Desmonta LTFS com lock exclusivo")
    group.add_argument("--deep-recovery", action="store_true", help="Executa ltfsck --deep-recovery com lock exclusivo")
    group.add_argument("--force-mount-ro", action="store_true", help="Monta LTFS em RO ignorando EOD ausente (force_mount_no_eod)")
    group.add_argument("--erase-history", action="store_true", help="Executa ltfsck --erase-history: reconstrói índice descartando histórico")
    group.add_argument("--rollback-generation0", action="store_true",
                       help="ltfsck -r -g 0 -j: rollback para geração 0 + apaga histórico — cria volume vazio e montável")
    group.add_argument("--reformat", action="store_true",
                       help="Reformata a fita com mkltfs — apaga TODOS os dados permanentemente; requer --volser")
    # cursor — write checkpoint / resume
    group.add_argument("--cursor-open", action="store_true", help="Abre sessão de escrita e registra bloco inicial (requer --volser)")
    group.add_argument("--cursor-update", action="store_true", help="Atualiza cursor após gravar arquivo (requer --volser --file)")
    group.add_argument("--cursor-close", action="store_true", help="Encerra sessão de escrita com status limpo (requer --volser)")
    group.add_argument("--cursor-status", action="store_true", help="Exibe estado do cursor de escrita (requer --volser)")
    group.add_argument("--cursor-recover", action="store_true", help="Recovery a partir do cursor: ltfsck + lista de re-fila (requer --volser)")
    group.add_argument("--cursor-list", action="store_true", help="Lista todos os cursores ativos no servidor")

    parser.add_argument("--volser", default="", help="Volser da fita (ex: NC2508) — obrigatório para modos cursor-*")
    parser.add_argument("--file", dest="file_path", default="", help="Caminho do arquivo gravado — usado com --cursor-update")
    parser.add_argument("--block", type=int, default=None, help="Bloco de fita explícito — usado com --cursor-update")
    parser.add_argument("--session-id", default=None, help="ID da sessão de escrita — usado com --cursor-open")

    args = parser.parse_args()
    if args.debug:
        DEBUG = True
        LOGGER.setLevel(logging.DEBUG)
        LOGGER.debug("Modo debug ativado — device=%s tape=%s mount=%s", LTFS_DEVICE, LTFS_TAPE_DEVICE, LTFS_MOUNT_POINT)

    mode = next(
        flag.replace("_", "-")
        for flag, enabled in vars(args).items()
        if isinstance(enabled, bool) and enabled and flag not in {"debug"}
    )
    result = run_mode(mode, volser=args.volser, file_path=args.file_path, block=args.block, session_id=args.session_id)
    if not result["success"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
