#!/usr/bin/env python3
"""Exporter Prometheus do LTO-6 Tape Manager (porta EXPORTER_PORT, default 9125).

Expõe métricas do buffer pré-tape, estado do mount/serviço, temperatura do drive
e atividade do button-watch. Formato textfile-compatível em /metrics.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("EXPORTER_PORT", "9125"))
DRIVE = os.getenv("DRIVE_ID", "lto6")
BUFFER_PATH = os.getenv("BUFFER_PATH", "")
BUFFER_MIN_FREE_GIB = int(os.getenv("BUFFER_MIN_FREE_GIB", "30"))
BUFFER_GATE_PCT = int(os.getenv("BUFFER_GATE_PCT", "80"))
MOUNT_POINT = os.getenv("LTFS_MOUNT_POINT", "/mnt/tape/lto6")
SERVICE = os.getenv("LTFS_SERVICE", "ltfs-lto6.service")
SG = os.getenv("LTFS_DEVICE", "/dev/sg4")


def _host(*cmd: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", *cmd],
                              capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        return subprocess.CompletedProcess(cmd, 1, "", "")


def _metric(name: str, value, labels: str = "") -> str:
    lbl = "{%s,drive=\"%s\"}" % (labels, DRIVE) if labels else "{drive=\"%s\"}" % DRIVE
    return f"{name}{lbl} {value}\n"


def collect() -> str:
    out = []

    # ── buffer ──
    if BUFFER_PATH and os.path.isdir(BUFFER_PATH):
        u = shutil.disk_usage(BUFFER_PATH)
        free_gib = u.free / (1024**3)
        pct = (u.used / u.total * 100) if u.total else 0
        lbl = 'dataset="%s"' % BUFFER_PATH
        out.append(_metric("lto6_app_buffer_bytes_free", u.free, lbl))
        out.append(_metric("lto6_app_buffer_pct_used", round(pct, 2), lbl))
        out.append(_metric("lto6_app_buffer_gate_ok", 1 if pct < BUFFER_GATE_PCT else 0, lbl))
    else:
        out.append(_metric("lto6_app_buffer_gate_ok", 0, 'dataset="%s"' % BUFFER_PATH))

    # ── mount / serviço (consultados no host) ──
    mnt = _host("findmnt", MOUNT_POINT)
    out.append(_metric("lto6_app_mount_up", 1 if mnt.returncode == 0 else 0))
    svc = _host("systemctl", "is-active", SERVICE)
    out.append(_metric("lto6_app_service_up", 1 if svc.stdout.strip() == "active" else 0))
    bw = _host("systemctl", "is-active", "ltfs-button-watch.service")
    out.append(_metric("lto6_app_button_watch_up", 1 if bw.stdout.strip() == "active" else 0))

    # ── temperatura do drive (log page 0x0d) ──
    temp = _host("sg_logs", "-p", "0x0d", SG)
    m = re.search(r"Current temperature[^0-9]*(\d+)", temp.stdout)
    if m:
        out.append(_metric("lto6_app_drive_temp_celsius", int(m.group(1))))

    out.append(_metric("lto6_app_last_scrape_timestamp", int(time.time())))
    return "".join(out)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silencioso
        pass

    def do_GET(self):
        if self.path not in ("/metrics", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = collect().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
