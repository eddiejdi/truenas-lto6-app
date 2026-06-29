#!/usr/bin/env python3
"""Daemon de watch de botão e inserção automática de fita LTO.

Monitora drives LTO via polling SCSI e chama o orchestrator
(ltfs_recovery.py) para montar/desmontar de forma segura.

Fluxo de ejeção (botão físico pressionado):
  1. Unit Attention ASC=5a/01 detectado (PREVENT MEDIUM REMOVAL ativo)
  2. ltfs_recovery.py --orchestrated-stop (com env do drive)
  3. allow-removal + rewind + eject mecânico
  4. Notificação Telegram

Fluxo de auto-mount (fita inserida):
  1. sg_turs detecta medium present
  2. Aguarda SETTLE_DELAY segundos para estabilização do drive
  3. ltfs_recovery.py --orchestrated-mount (com env do drive)
  4. Notificação Telegram

Configuração lida de /etc/default/ltfs-lto6 e /etc/default/ltfs-lto6b:
  LTFS_DEVICE, LTFS_TAPE_DEVICE, LTFS_SERVICE, LTFS_MOUNT_POINT

Uso:
    python3 ltfs_button_watch.py          # daemon (todos os drives)
    python3 ltfs_button_watch.py --list   # listar drives configurados
    python3 ltfs_button_watch.py --status # estado atual
    python3 ltfs_button_watch.py --debug  # log detalhado
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("ltfs-button-watch")

# ── Constantes ────────────────────────────────────────────────────────
DRIVE_ENV_GLOB = "/etc/default/ltfs-lto6*"
RECOVERY_SCRIPT = Path(os.getenv("LTFS_RECOVERY_SCRIPT", "/var/db/ltfs-tools/ltfs_recovery.py"))
POLL_INTERVAL = int(os.getenv("LTFS_BUTTON_POLL_INTERVAL", "3"))      # segundos entre polls
SETTLE_DELAY = int(os.getenv("LTFS_INSERT_SETTLE_DELAY", "15"))       # espera após inserção
REWIND_TIMEOUT = 600
EJECT_TIMEOUT = 120
ORCH_TIMEOUT = 900   # 15 min para operações do orchestrator

# ── Telegram (opcional) ───────────────────────────────────────────────
_tg_lock = threading.Lock()


def _load_telegram_creds() -> tuple[str, str]:
    """Lê token e chat_id do env do orchestrator."""
    env_file = Path("/etc/default/ltfs-recovery")
    env: Dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
    token = env.get("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN", ""))
    chat_id = env.get("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID", ""))
    return token, chat_id


def _notify(msg: str) -> None:
    """Envia mensagem Telegram de forma não-bloqueante."""
    def _send() -> None:
        token, chat_id = _load_telegram_creds()
        if not token or not chat_id:
            return
        payload = json.dumps({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with _tg_lock:
                urllib.request.urlopen(req, timeout=10)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Telegram notification failed: %s", exc)

    threading.Thread(target=_send, daemon=True).start()


# ── Primitivas SCSI ────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("Timeout (%ds): %s", timeout, " ".join(cmd))
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"not found: {cmd[0]}")


def _has_medium(sg_dev: str) -> bool:
    r = _run(["sg_turs", sg_dev], timeout=10)
    if r.returncode != 0:
        combined = r.stdout + r.stderr
        if "medium not present" in combined.lower() or "not ready" in combined.lower():
            return False
    return r.returncode == 0


def _eject_button_pressed(sg_dev: str) -> bool:
    """Detecta Unit Attention ASC=5a/01 (operador pressionou botão com PREVENT ativo)."""
    r = _run(["sg_turs", "-v", sg_dev], timeout=5)
    combined = r.stdout + r.stderr
    if "5a" in combined.lower() and "unit attention" in combined.lower():
        return True
    return False


def _prevent_removal(sg_dev: str, prevent: bool) -> bool:
    flag = "--prevent=1" if prevent else "--allow"
    r = _run(["sg_prevent", flag, sg_dev], timeout=10)
    action = "bloqueado" if prevent else "desbloqueado"
    if r.returncode == 0:
        logger.debug("Eject %s: %s", action, sg_dev)
    return r.returncode == 0


# ── Watcher por drive ─────────────────────────────────────────────────

class DriveWatcher:
    """Monitora um drive LTO e reage a eventos via orchestrator."""

    def __init__(self, env_file: Path) -> None:
        self.env_file = env_file
        self.env = self._load_env(env_file)
        self.sg_dev: str = self.env.get("LTFS_DEVICE", "")
        self.nst_dev: str = self.env.get("LTFS_TAPE_DEVICE", "")
        self.service: str = self.env.get("LTFS_SERVICE", "ltfs-lto6.service")
        self.mount_point: str = self.env.get("LTFS_MOUNT_POINT", "/mnt/tape/lto6")
        self.label: str = env_file.name  # "ltfs-lto6" ou "ltfs-lto6b"

        self._has_medium: bool = False
        self._locked: bool = False   # PREVENT MEDIUM REMOVAL ativo
        self._op_lock = threading.Lock()  # impede operações paralelas no mesmo drive

    def _load_env(self, path: Path) -> Dict[str, str]:
        env: Dict[str, str] = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'\"")
        return env

    def _orch_env(self) -> Dict[str, str]:
        """Monta env para subprocess do orchestrator com vars do drive."""
        env = os.environ.copy()
        env.update(self.env)
        return env

    def _call_orchestrator(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, str(RECOVERY_SCRIPT), *args]
        logger.debug("[%s] Chamando orchestrator: %s", self.label, " ".join(args))
        try:
            return subprocess.run(
                cmd, env=self._orch_env(),
                capture_output=True, text=True, timeout=ORCH_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            logger.error("[%s] Timeout (%ds) no orchestrator", self.label, ORCH_TIMEOUT)
            return subprocess.CompletedProcess(cmd, 124, "", "timeout")

    def _mechanical_eject(self) -> None:
        """Allow removal, rewind e eject mecânico."""
        _prevent_removal(self.sg_dev, False)
        self._locked = False
        logger.info("[%s] Rebobinando...", self.label)
        _run(["mt", "-f", self.nst_dev, "rewind"], timeout=REWIND_TIMEOUT)
        logger.info("[%s] Ejetando...", self.label)
        r = _run(["mt", "-f", self.nst_dev, "eject"], timeout=EJECT_TIMEOUT)
        if r.returncode != 0:
            logger.warning("[%s] mt eject falhou, tentando sg_start", self.label)
            _run(["sg_start", "--eject", self.sg_dev], timeout=EJECT_TIMEOUT)

    def _handle_eject(self) -> None:
        """Botão de eject: orchestrated-stop + eject mecânico."""
        logger.info("[%s] === BOTÃO DE EJECT DETECTADO ===", self.label)
        _notify(f"\U0001f4fc Fita {self.label}: botão pressionado — desmontando...")

        r = self._call_orchestrator("--orchestrated-stop")
        result = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        success = result.get("success", r.returncode == 0)

        if success:
            logger.info("[%s] Desmontagem concluída — iniciando eject mecânico", self.label)
            self._mechanical_eject()
            logger.info("[%s] Fita ejetada com sucesso ✓", self.label)
            _notify(f"✅ {self.label}: fita ejetada com sucesso")
        else:
            msg = result.get("message", r.stderr[:200])
            logger.error("[%s] Falha na desmontagem: %s", self.label, msg)
            _notify(f"❌ {self.label}: falha ao desmontar — {msg[:100]}")
            if _has_medium(self.sg_dev):
                _prevent_removal(self.sg_dev, True)
                self._locked = True

    def _handle_insert(self) -> None:
        """Fita inserida: aguarda estabilização, depois orchestrated-mount."""
        logger.info("[%s] Fita inserida — aguardando %ds para estabilização", self.label, SETTLE_DELAY)
        _notify(f"\U0001f4fc Fita inserida em {self.label} — montando em {SETTLE_DELAY}s...")
        time.sleep(SETTLE_DELAY)

        if not _has_medium(self.sg_dev):
            logger.info("[%s] Fita removida antes do mount — abortando", self.label)
            return

        _prevent_removal(self.sg_dev, True)
        self._locked = True

        r = self._call_orchestrator("--orchestrated-mount")
        result = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
        success = result.get("success", r.returncode == 0)

        if success:
            logger.info("[%s] Fita montada com sucesso em %s ✓", self.label, self.mount_point)
            _notify(f"✅ {self.label}: fita montada em {self.mount_point}")
        else:
            msg = result.get("message", r.stderr[:200])
            logger.error("[%s] Falha no mount: %s", self.label, msg)
            _notify(f"❌ {self.label}: falha ao montar — {msg[:100]}")
            _prevent_removal(self.sg_dev, False)
            self._locked = False

    def _init_state(self) -> None:
        """Detecta estado inicial ao subir o daemon."""
        if not self.sg_dev or not Path(self.sg_dev).exists():
            logger.warning("[%s] Device %s não encontrado — drive ignorado", self.label, self.sg_dev)
            return
        present = _has_medium(self.sg_dev)
        self._has_medium = present
        if present:
            _prevent_removal(self.sg_dev, True)
            self._locked = True
            logger.info("[%s] Fita já presente ao iniciar — PREVENT ativado", self.label)
        else:
            logger.info("[%s] Nenhuma fita no drive ao iniciar", self.label)

    def poll(self) -> None:
        """Um ciclo de poll. Chamado periodicamente pelo loop principal."""
        if not self.sg_dev or not Path(self.sg_dev).exists():
            return

        was_present = self._has_medium
        now_present = _has_medium(self.sg_dev)
        self._has_medium = now_present

        if not was_present and now_present:
            # Fita inserida
            if self._op_lock.acquire(blocking=False):
                def _run_insert() -> None:
                    try:
                        self._handle_insert()
                    finally:
                        self._op_lock.release()
                threading.Thread(target=_run_insert, daemon=True, name=f"insert-{self.label}").start()

        elif was_present and not now_present:
            # Fita removida externamente (após eject bem-sucedido ou manual)
            self._locked = False
            logger.info("[%s] Fita removida", self.label)

        elif now_present and self._locked:
            # Fita presente com PREVENT ativo — checar botão
            if _eject_button_pressed(self.sg_dev):
                if self._op_lock.acquire(blocking=False):
                    def _run_eject() -> None:
                        try:
                            self._handle_eject()
                        finally:
                            self._op_lock.release()
                    threading.Thread(target=_run_eject, daemon=True, name=f"eject-{self.label}").start()
                else:
                    logger.info("[%s] Operação já em andamento — botão ignorado", self.label)

        elif now_present and not self._locked:
            # Fita presente mas sem PREVENT (pode ter ficado desbloqueado após falha)
            _prevent_removal(self.sg_dev, True)
            self._locked = True

    def status(self) -> Dict[str, Any]:
        present = _has_medium(self.sg_dev) if Path(self.sg_dev).exists() else None
        return {
            "drive": self.label,
            "sg_dev": self.sg_dev,
            "nst_dev": self.nst_dev,
            "service": self.service,
            "mount_point": self.mount_point,
            "has_medium": present,
            "prevent_removal": self._locked,
            "operating": self._op_lock.locked(),
        }


# ── Daemon principal ──────────────────────────────────────────────────

class ButtonWatchDaemon:
    def __init__(self) -> None:
        self.watchers: list[DriveWatcher] = []
        self._running = True
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: Any) -> None:
        logger.info("Sinal %d recebido — encerrando daemon", signum)
        self._running = False

    def _load_watchers(self) -> None:
        import glob
        for path_str in sorted(glob.glob(DRIVE_ENV_GLOB)):
            path = Path(path_str)
            try:
                w = DriveWatcher(path)
                if not w.sg_dev:
                    logger.warning("LTFS_DEVICE ausente em %s — ignorado", path)
                    continue
                self.watchers.append(w)
                logger.info("Drive configurado: %s → %s (%s)", path.name, w.sg_dev, w.service)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Erro ao carregar %s: %s", path, exc)

    def run(self) -> None:
        self._load_watchers()
        if not self.watchers:
            logger.error("Nenhum drive configurado em %s", DRIVE_ENV_GLOB)
            sys.exit(1)

        for w in self.watchers:
            w._init_state()

        logger.info(
            "Daemon iniciado — monitorando %d drive(s), poll a cada %ds",
            len(self.watchers), POLL_INTERVAL,
        )
        _notify(f"\U0001f7e2 ltfs-button-watch iniciado ({len(self.watchers)} drives)")

        while self._running:
            for w in self.watchers:
                try:
                    w.poll()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("[%s] Erro no poll: %s", w.label, exc)
            time.sleep(POLL_INTERVAL)

        # Cleanup: desbloquear drives ao sair
        for w in self.watchers:
            if w._locked:
                _prevent_removal(w.sg_dev, False)
        logger.info("Daemon encerrado")


# ── CLI ───────────────────────────────────────────────────────────────

def cmd_list() -> None:
    import glob
    print(f"{'Drive':<18} {'sg_dev':<12} {'nst_dev':<12} {'Service':<25} {'Mountpoint'}")
    print("─" * 95)
    for path_str in sorted(glob.glob(DRIVE_ENV_GLOB)):
        try:
            w = DriveWatcher(Path(path_str))
            print(f"{w.label:<18} {w.sg_dev:<12} {w.nst_dev:<12} {w.service:<25} {w.mount_point}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERRO ao ler {path_str}: {exc}")


def cmd_status() -> None:
    import glob
    for path_str in sorted(glob.glob(DRIVE_ENV_GLOB)):
        try:
            w = DriveWatcher(Path(path_str))
            w._init_state()
            s = w.status()
            medium = "presente" if s["has_medium"] else "ausente"
            prevent = "PREVENT ativo" if s["prevent_removal"] else "livre"
            print(f"{s['drive']:18} {s['sg_dev']:8} fita={medium:8} {prevent}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ERRO: {path_str}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Daemon de watch para botão/inserção de fita LTO")
    parser.add_argument("--list", action="store_true", help="Listar drives configurados")
    parser.add_argument("--status", action="store_true", help="Estado atual dos drives")
    parser.add_argument("--debug", action="store_true", help="Log detalhado (DEBUG)")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list:
        cmd_list()
        return

    if args.status:
        cmd_status()
        return

    ButtonWatchDaemon().run()


if __name__ == "__main__":
    main()
