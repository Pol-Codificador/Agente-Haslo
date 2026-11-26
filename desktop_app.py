"""Ventana nativa y coordinador de servicios de Vortex AI Desktop."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root / relative


DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "VortexAI"
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "temp").mkdir(exist_ok=True)
(DATA_DIR / "whatsapp_bridge").mkdir(exist_ok=True)
os.environ["VORTEX_DATA_DIR"] = str(DATA_DIR)

if not (DATA_DIR / ".env").exists():
    template = resource_path(".env.example")
    if template.exists():
        shutil.copy2(template, DATA_DIR / ".env")


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class ServiceManager:
    def __init__(self) -> None:
        self.server = None
        self.server_thread: threading.Thread | None = None
        self.whatsapp: subprocess.Popen | None = None
        self.owns_backend = False

    def start(self) -> None:
        if not port_open(8000):
            import uvicorn
            from agent_core.server import app

            config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="warning")
            self.server = uvicorn.Server(config)
            self.server_thread = threading.Thread(target=self.server.run, daemon=True)
            self.server_thread.start()
            self.owns_backend = True
        self.start_whatsapp()

    def start_whatsapp(self) -> None:
        bridge = resource_path("whatsapp_bridge/index.js")
        bundled_node = resource_path("node.exe")
        node = bundled_node if bundled_node.exists() else Path(shutil.which("node") or "")
        if not bridge.exists() or not node.exists():
            return
        os.environ["VORTEX_NODE_EXE"] = str(node)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.whatsapp = subprocess.Popen(
            [str(node), str(bridge)],
            cwd=str(bridge.parent),
            env=os.environ.copy(),
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.whatsapp and self.whatsapp.poll() is None:
            self.whatsapp.terminate()
            try:
                self.whatsapp.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.whatsapp.kill()
        if self.server and self.owns_backend:
            self.server.should_exit = True


def main() -> int:
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtGui import QDesktopServices, QIcon
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from PySide6.QtWidgets import QApplication, QMainWindow

    class VortexPage(QWebEnginePage):
        def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
            if url.host() in {"127.0.0.1", "localhost", ""}:
                return True
            QDesktopServices.openUrl(url)
            return False

    class MainWindow(QMainWindow):
        def __init__(self, services: ServiceManager) -> None:
            super().__init__()
            self.services = services
            self.setWindowTitle("Vortex AI Desktop")
            self.setMinimumSize(1024, 700)
            self.resize(1380, 880)
            icon = resource_path("assets/vortex-ai-logo.png")
            if icon.exists():
                self.setWindowIcon(QIcon(str(icon)))
            self.web = QWebEngineView(self)
            self.web.setPage(VortexPage(self.web))
            self.setCentralWidget(self.web)
            self._attempts = 0
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.load_when_ready)
            self.timer.start(250)

        def load_when_ready(self) -> None:
            self._attempts += 1
            try:
                urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=0.3).close()
                self.timer.stop()
                self.web.setUrl(QUrl("http://127.0.0.1:8000/dashboard#overview"))
            except Exception:
                if self._attempts > 120:
                    self.timer.stop()
                    self.web.setHtml("<h2>Vortex no pudo iniciar el servicio local.</h2>")

        def closeEvent(self, event) -> None:
            self.services.stop()
            event.accept()

    services = ServiceManager()
    services.start()
    app = QApplication(sys.argv)
    app.setApplicationName("Vortex AI Desktop")
    app.setOrganizationName("Vortex AI")
    window = MainWindow(services)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
