"""Lanzador silencioso del Vortex AI Desktop (FastAPI + WhatsApp Bridge + Brave App Mode)."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib import request as urllib_request

import psutil

from agent_core.config import TEMP_DIR, get_env

ROOT_DIR = Path(__file__).resolve().parent
VENV_PYTHON = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
WHATSAPP_DIR = ROOT_DIR / "whatsapp_bridge"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
DAEMON_FLAGS = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

AGENT_HOST = get_env("AGENT_HOST", "127.0.0.1") or "127.0.0.1"
AGENT_PORT = int(get_env("AGENT_PORT", "8000") or "8000")
BASE_URL = f"http://{AGENT_HOST}:{AGENT_PORT}"
DASHBOARD_URL = f"{BASE_URL}/dashboard"


def _show_error(title: str, msg: str) -> None:
    """Muestra un cuadro de error nativo solo si el arranque falla."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, msg)
        root.destroy()
    except Exception:
        print(f"[{title}] {msg}", file=sys.stderr)


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((AGENT_HOST, port)) != 0


def _kill_pid_tree(pid: int) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=5,
            check=False,
        )
    except Exception:
        pass


def cleanup_orphan_processes() -> bool:
    """Cierra procesos previos del servidor FastAPI y del puente WhatsApp."""
    pids: set[int] = set()
    wa_dir_resolved = WHATSAPP_DIR.resolve()
    root_resolved = ROOT_DIR.resolve()

    for conn in psutil.net_connections(kind="inet"):
        try:
            if conn.laddr and conn.laddr.port == AGENT_PORT and conn.pid:
                if conn.pid != os.getpid():
                    pids.add(conn.pid)
        except Exception:
            continue

    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = proc.info.get("pid") or 0
            if pid <= 0 or pid == os.getpid():
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()

            if name in ("python.exe", "pythonw.exe") and (
                "agent_core.server" in cmdline
                or "agent_core\\server.py" in cmdline
                or "uvicorn" in cmdline
            ):
                pids.add(pid)
            elif name in ("node.exe", "node"):
                is_wa = "whatsapp_bridge" in cmdline
                if not is_wa and "index.js" in cmdline:
                    try:
                        cwd = Path(proc.cwd()).resolve()
                        if cwd in (wa_dir_resolved, root_resolved):
                            is_wa = True
                    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                        pass
                if is_wa:
                    pids.add(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    for pid in pids:
        _kill_pid_tree(pid)

    if pids:
        time.sleep(0.5)
    return _is_port_free(AGENT_PORT)


def _find_brave_exe() -> str | None:
    local_app = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.join(local_app, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return shutil.which("brave") or shutil.which("brave.exe")


def _find_edge_exe() -> str | None:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return shutil.which("msedge")


def open_dashboard_app(url: str) -> str:
    """Abre el Dashboard como ventana nativa de escritorio en modo --app."""
    brave = _find_brave_exe()
    if brave:
        subprocess.Popen(
            [brave, f"--app={url}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Brave (--app): {brave}"

    edge = _find_edge_exe()
    if edge:
        subprocess.Popen(
            [edge, f"--app={url}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return f"Edge (--app): {edge}"

    webbrowser.open(url)
    return "Navegador predeterminado"


def wait_for_backend(timeout_sec: int = 25) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(f"{BASE_URL}/health", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def launch(open_browser: bool = True) -> int:
    if not VENV_PYTHON.exists():
        _show_error("Entorno no encontrado", f"No se encontró Python en:\n{VENV_PYTHON}")
        return 1

    if not WHATSAPP_DIR.exists():
        _show_error("Carpeta faltante", f"No se encontró:\n{WHATSAPP_DIR}")
        return 1

    cleanup_orphan_processes()
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    backend_log = open(TEMP_DIR / "backend.log", "w", encoding="utf-8", errors="replace")
    wa_log = open(TEMP_DIR / "whatsapp.log", "w", encoding="utf-8", errors="replace")

    backend_proc = subprocess.Popen(
        [
            str(VENV_PYTHON),
            "-u",
            "-m",
            "uvicorn",
            "agent_core.server:app",
            "--host",
            AGENT_HOST,
            "--port",
            str(AGENT_PORT),
        ],
        cwd=str(ROOT_DIR),
        env=env,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=DAEMON_FLAGS,
    )

    if not wait_for_backend(timeout_sec=25):
        _kill_pid_tree(backend_proc.pid)
        _show_error(
            "Error al iniciar FastAPI",
            f"El servidor no respondió en {BASE_URL}/health.\nRevisa {TEMP_DIR / 'backend.log'}.",
        )
        return 1

    subprocess.Popen(
        ["node", "index.js"],
        cwd=str(WHATSAPP_DIR),
        env=env,
        stdout=wa_log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=DAEMON_FLAGS,
    )

    if open_browser:
        open_dashboard_app(DASHBOARD_URL)

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Lanzador Vortex AI Desktop")
    parser.add_argument("--no-browser", action="store_true", help="No abrir ventana de Brave")
    args = parser.parse_args()
    sys.exit(launch(open_browser=not args.no_browser))


if __name__ == "__main__":
    main()
