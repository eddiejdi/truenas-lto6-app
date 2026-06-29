#!/usr/bin/env python3
"""Orchestrator API — HTTP wrapper fino sobre ltfs_recovery.py.

Toda operação de fita passa por ltfs_recovery.py (que já detém o lock exclusivo
LTFS_ORCH_LOCK e o conhecimento de recovery). As rotas de mount revalidam o gate
de buffer pré-tape antes de prosseguir.

Endpoints (porta ORCHESTRATOR_PORT, default 9877):
    GET  /health         → liveness
    GET  /status         → --check + estado do buffer
    GET  /diagnose       → --diagnose
    GET  /cursor         → --cursor-list
    POST /mount          → --orchestrated-mount  (valida buffer antes)
    POST /unmount        → --orchestrated-stop
    POST /eject          → --orchestrated-stop + eject mecânico (via host)
    POST /deep-recovery  → --deep-recovery
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [orch-api] %(message)s")
log = logging.getLogger("orch-api")

PORT = int(os.getenv("ORCHESTRATOR_PORT", "9877"))
RECOVERY = os.getenv("LTFS_RECOVERY", "/opt/lto6/ltfs_recovery.py")
BUFFER_PATH = os.getenv("BUFFER_PATH", "")
BUFFER_MIN_FREE_GIB = int(os.getenv("BUFFER_MIN_FREE_GIB", "30"))
NST = os.getenv("LTFS_TAPE_DEVICE", "/dev/nst1")

ORCH_TIMEOUT = 900


def _buffer_state() -> dict:
    if not BUFFER_PATH or not os.path.isdir(BUFFER_PATH):
        return {"ok": False, "reason": "buffer ausente", "path": BUFFER_PATH}
    usage = shutil.disk_usage(BUFFER_PATH)
    free_gib = usage.free / (1024**3)
    pct_used = usage.used / usage.total * 100 if usage.total else 0
    return {
        "ok": free_gib >= BUFFER_MIN_FREE_GIB,
        "path": BUFFER_PATH,
        "free_gib": round(free_gib, 1),
        "pct_used": round(pct_used, 1),
        "min_free_gib": BUFFER_MIN_FREE_GIB,
    }


def _run_recovery(*flags: str) -> dict:
    cmd = [sys.executable, RECOVERY, *flags]
    log.info("exec %s", " ".join(flags))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=ORCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"success": False, "message": f"timeout após {ORCH_TIMEOUT}s"}
    out = r.stdout.strip()
    if out.startswith("{"):
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
    return {"success": r.returncode == 0, "message": out or r.stderr.strip(),
            "returncode": r.returncode}


def _host(*cmd: str) -> subprocess.CompletedProcess:
    """Executa no namespace do host via nsenter."""
    return subprocess.run(["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", *cmd],
                          capture_output=True, text=True, timeout=180)


def _mechanical_eject() -> dict:
    r1 = _host("mt", "-f", NST, "rewind")
    r2 = _host("mt", "-f", NST, "eject")
    ok = r2.returncode == 0
    if not ok:
        r3 = _host("sg_start", "--eject", os.getenv("LTFS_DEVICE", "/dev/sg4"))
        ok = r3.returncode == 0
    return {"success": ok, "rewind_rc": r1.returncode, "eject_rc": r2.returncode}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silencia log default
        log.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/status":
            res = _run_recovery("--check")
            res["buffer"] = _buffer_state()
            return self._send(200, res)
        if self.path == "/diagnose":
            return self._send(200, _run_recovery("--diagnose"))
        if self.path == "/cursor":
            return self._send(200, _run_recovery("--cursor-list"))
        return self._send(404, {"error": "not found", "path": self.path})

    def do_POST(self):
        if self.path == "/mount":
            buf = _buffer_state()
            if not buf["ok"]:
                return self._send(409, {"success": False,
                                        "message": "gate de buffer bloqueou o mount", "buffer": buf})
            res = _run_recovery("--orchestrated-mount")
            res["buffer"] = buf
            return self._send(200 if res.get("success") else 500, res)
        if self.path == "/unmount":
            return self._send(200, _run_recovery("--orchestrated-stop"))
        if self.path == "/eject":
            stop = _run_recovery("--orchestrated-stop")
            if not stop.get("success"):
                return self._send(500, {"success": False, "stage": "unmount", "detail": stop})
            ej = _mechanical_eject()
            return self._send(200 if ej["success"] else 500,
                              {"success": ej["success"], "unmount": stop, "eject": ej})
        if self.path == "/deep-recovery":
            return self._send(200, _run_recovery("--deep-recovery"))
        return self._send(404, {"error": "not found", "path": self.path})


def main() -> None:
    log.info("Orchestrator API ouvindo na :%d (recovery=%s)", PORT, RECOVERY)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
