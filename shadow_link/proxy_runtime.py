"""Shared lifecycle manager for Shadow Link's mitmproxy process."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import site
import socket
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path


def get_audit_dir() -> Path:
    configured = os.getenv("SHADOW_LINK_AUDIT_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parent.parent / "claude_audit"


def read_proxy_audit(limit: int = 10) -> dict:
    """Read the newest proxy audit events across daily JSONL files."""
    limit = max(1, min(int(limit), 50))
    directory = get_audit_dir()
    events = []
    files = sorted(directory.glob("*.jsonl"), reverse=True) if directory.exists() else []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, dict):
                events.append(event)
            if len(events) >= limit:
                break
        if len(events) >= limit:
            break
    return {
        "audit_dir": str(directory),
        "events": events,
        "files": len(files),
        "limit": limit,
    }


def resolve_mitmdump() -> Path | None:
    """Locate mitmdump in the active interpreter, user install, or PATH."""
    executable = "mitmdump.exe" if sys.platform == "win32" else "mitmdump"
    candidates = [
        Path(sys.executable).resolve().parent / executable,
        Path(sysconfig.get_path("scripts")) / executable,
        Path(site.getuserbase())
        / f"Python{sys.version_info.major}{sys.version_info.minor}"
        / "Scripts"
        / executable,
    ]
    path_match = shutil.which("mitmdump")
    if path_match:
        candidates.append(Path(path_match))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


class ProxyRuntime:
    """Own at most one background mitmdump process for the current app."""

    def __init__(self):
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._port: int | None = None

    def status(self, port: int | None = None) -> dict:
        requested_port = int(port or self._port or os.getenv("SHADOW_LINK_PROXY_PORT", "8080"))
        with self._lock:
            process = self._process
            if process is not None and process.poll() is not None:
                self._process = None
                self._port = None
                process = None
            managed = process is not None
            pid = process.pid if process is not None else None
            active_port = self._port if managed else requested_port
        listening = _port_open(int(active_port))
        binary = resolve_mitmdump()
        return {
            "running": bool(managed or listening),
            "managed": managed,
            "listening": listening,
            "port": int(active_port),
            "pid": pid,
            "available": binary is not None,
            "binary": str(binary) if binary else None,
            "proxy_url": f"http://127.0.0.1:{active_port}",
        }

    def start(self, port: int | None = None) -> dict:
        requested_port = int(port or os.getenv("SHADOW_LINK_PROXY_PORT", "8080"))
        current = self.status(requested_port)
        if current["managed"]:
            current["message"] = "Proxy is already managed by this process."
            return current
        if current["listening"]:
            raise RuntimeError(f"Port {requested_port} is already in use by another process")

        binary = resolve_mitmdump()
        if binary is None:
            raise RuntimeError(
                "mitmdump was not found in the active Python Scripts directory or PATH"
            )
        addon = Path(__file__).resolve().parent / "proxy.py"
        command = [
            str(binary), "-s", str(addon), "--listen-port", str(requested_port), "-q",
        ]
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        time.sleep(0.35)
        if process.poll() is not None:
            raise RuntimeError(f"mitmdump exited during startup with code {process.returncode}")
        with self._lock:
            self._process = process
            self._port = requested_port
        result = self.status(requested_port)
        result["message"] = "Proxy started."
        return result

    def stop(self) -> dict:
        with self._lock:
            process = self._process
            port = self._port or int(os.getenv("SHADOW_LINK_PROXY_PORT", "8080"))
            self._process = None
            self._port = None
        if process is None:
            result = self.status(port)
            result["stopped"] = False
            result["message"] = (
                "A proxy is listening but is not managed by this process."
                if result["listening"] else "No managed proxy is running."
            )
            return result
        process.terminate()
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
        deadline = time.monotonic() + 3.0
        while _port_open(int(port)) and time.monotonic() < deadline:
            time.sleep(0.1)
        result = self.status(port)
        result["stopped"] = True
        result["message"] = "Proxy stopped."
        return result


PROXY_RUNTIME = ProxyRuntime()
atexit.register(PROXY_RUNTIME.stop)
