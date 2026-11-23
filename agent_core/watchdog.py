"""Vigilante Proactivo en segundo plano con alertas push a WhatsApp."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import psutil
import requests

from agent_core.config import LIMA_TZ, get_env

MAX_ALERTS = 50


def _get_notify_url() -> str:
    port = get_env("WHATSAPP_NOTIFY_PORT", "8001") or "8001"
    return f"http://127.0.0.1:{port}/notify"


def send_proactive_whatsapp(
    message: str,
    image_path: str | None = None,
    file_path: str | None = None,
    images: list[str] | None = None,
    files: list[str] | None = None,
) -> tuple[bool, str]:
    """Envía un mensaje proactivo (texto, imágenes o documentos) a WhatsApp vía Node.js."""
    payload: dict[str, Any] = {"message": (message or "").strip() or "Alerta del sistema"}
    if image_path:
        payload["image_path"] = image_path
    if file_path:
        payload["file_path"] = file_path
    if images:
        payload["images"] = [str(p) for p in images if p]
    if files:
        payload["files"] = [str(p) for p in files if p]

    try:
        resp = requests.post(_get_notify_url(), json=payload, timeout=12)
        if resp.ok:
            return True, "Notificación enviada por WhatsApp"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        return False, f"No se pudo conectar al notificador WhatsApp: {exc}"


def parse_timer_seconds(target_value: str) -> int:
    """Convierte expresiones ('5', '10m', '30s', '1h', '21:30') a segundos de espera."""
    raw = (target_value or "").strip().lower()
    if not raw:
        raise ValueError("Debes indicar duración (ej. '10', '30s', '1h') o hora ('21:30').")

    clock_match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if clock_match:
        hh, mm = int(clock_match.group(1)), int(clock_match.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError(f"Hora inválida: '{target_value}'.")
        now_lima = datetime.now(LIMA_TZ)
        target_dt = now_lima.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target_dt <= now_lima:
            target_dt += timedelta(days=1)
        return max(5, int((target_dt - now_lima).total_seconds()))

    unit_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(s|seg|segundos?|m|min|minutos?|h|hr|horas?)?", raw)
    if not unit_match:
        raise ValueError(f"No pude interpretar el tiempo '{target_value}'.")

    amount = float(unit_match.group(1).replace(",", "."))
    unit = (unit_match.group(2) or "m").lower()
    if unit.startswith("s"):
        seconds = int(round(amount))
    elif unit.startswith("h"):
        seconds = int(round(amount * 3600))
    else:
        seconds = int(round(amount * 60))

    return max(5, min(seconds, 7 * 24 * 3600))


def _find_matching_pids(target_value: str) -> list[int]:
    """Busca PIDs de procesos cuyo PID o nombre/línea de comandos coincida con target_value."""
    query = (target_value or "").strip().lower()
    if not query:
        return []
    if query.isdigit():
        pid = int(query)
        return [pid] if psutil.pid_exists(pid) else []

    pids: list[int] = []
    my_pid = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(proc.info.get("pid") or 0)
            if pid <= 0 or pid == my_pid:
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if query in name or query in cmdline:
                pids.append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return pids


def _extract_screenshot_path(result_text: str) -> str | None:
    match = re.search(r"'([^']+\.png)'", result_text or "")
    if match:
        path = Path(match.group(1))
        if path.exists():
            return str(path)
    return None


class WatchdogManager:
    """Gestor de alertas proactivas en segundo plano para el Dashboard y WhatsApp."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._alerts: list[dict[str, Any]] = []
        self._cancel_flags: dict[int, threading.Event] = {}
        self._event_hook: Callable[[str, str, str], None] | None = None
        self._seq = 0

    def set_event_hook(self, hook: Callable[[str, str, str], None] | None) -> None:
        """Permite registrar un callback global (ej. Timeline de Gemini) sin acoplamiento circular."""
        with self._lock:
            self._event_hook = hook

    def _emit_event(
        self,
        on_event: Callable[[str, str, str], None] | None,
        ev_type: str,
        title: str,
        detail: str,
    ) -> None:
        hook = on_event or self._event_hook
        if hook is not None:
            try:
                hook(ev_type, title, detail)
            except Exception:
                pass

    @staticmethod
    def _normalize_condition(condition_type: str) -> str:
        raw = (condition_type or "").strip().lower()
        aliases = {
            "timer": "timer",
            "temporizador": "timer",
            "alarma": "timer",
            "recordatorio": "timer",
            "process_finished": "process_finished",
            "process": "process_finished",
            "proceso": "process_finished",
            "battery_low": "battery_low",
            "battery": "battery_low",
            "bateria": "battery_low",
            "batería": "battery_low",
            "dutic_check": "dutic_check",
            "dutic": "dutic_check",
            "tareas": "dutic_check",
        }
        canonical = aliases.get(raw)
        if not canonical:
            raise ValueError(
                "Tipo de alerta inválido. Usa: 'timer', 'process_finished', 'battery_low' o 'dutic_check'."
            )
        return canonical

    def _update_alert(self, alert_id: int, **fields: Any) -> None:
        with self._lock:
            for item in self._alerts:
                if item["id"] == alert_id:
                    item.update(fields)
                    break

    def list_alerts(self, limit: int = 25) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(a) for a in self._alerts[:limit]]

    def cancel_alert(self, alert_id: int) -> bool:
        with self._lock:
            flag = self._cancel_flags.get(int(alert_id))
            if flag is not None:
                flag.set()
            for item in self._alerts:
                if item["id"] == int(alert_id) and item["status"] == "active":
                    item["status"] = "cancelled"
                    item["detail"] = "Cancelada por el usuario"
                    return True
        return False

    def _sleep_interruptible(self, total_seconds: int, cancel_event: threading.Event) -> bool:
        """Duerme `total_seconds` devolviendo True si se completó o False si fue cancelado."""
        deadline = time.monotonic() + total_seconds
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(1.0, max(0.05, remaining)))
        return not cancel_event.is_set()

    def _finalize_alert(
        self,
        alert_id: int,
        ok: bool,
        info: str,
        event_title: str,
        event_detail: str,
        on_event: Callable[[str, str, str], None] | None,
    ) -> None:
        self._update_alert(
            alert_id,
            status="triggered" if ok else "failed",
            triggered_at=datetime.now(LIMA_TZ).strftime("%H:%M:%S"),
            detail=info,
        )
        self._emit_event(on_event, "alert" if ok else "error", event_title, event_detail)

    def _run_timer_worker(
        self,
        alert_id: int,
        seconds: int,
        message: str,
        wants_capture: bool,
        cancel_event: threading.Event,
        screenshot_fn: Callable[[], str] | None,
        on_event: Callable[[str, str, str], None] | None,
    ) -> None:
        if not self._sleep_interruptible(seconds, cancel_event):
            return

        image_path: str | None = None
        if wants_capture and screenshot_fn is not None:
            try:
                cap_res = screenshot_fn()
                image_path = _extract_screenshot_path(cap_res)
            except Exception:
                image_path = None

        text = f"⏰ *Recordatorio Proactivo:*\n{message or 'Tiempo cumplido.'}"
        ok, info = send_proactive_whatsapp(text, image_path=image_path)
        self._finalize_alert(
            alert_id,
            ok,
            info,
            "Alerta Timer enviada" if ok else "Fallo envío de Timer",
            f"{message} ({info})",
            on_event,
        )

    def _run_process_worker(
        self,
        alert_id: int,
        target_value: str,
        initial_pids: list[int],
        message: str,
        cancel_event: threading.Event,
        on_event: Callable[[str, str, str], None] | None,
    ) -> None:
        while not cancel_event.is_set():
            alive = [pid for pid in initial_pids if psutil.pid_exists(pid)]
            if not alive:
                break
            if not self._sleep_interruptible(3, cancel_event):
                return

        if cancel_event.is_set():
            return

        text = (
            f"✅ *Proceso Finalizado ({target_value}):*\n"
            f"{message or 'El proceso monitoreado en tu PC acaba de terminar.'}"
        )
        ok, info = send_proactive_whatsapp(text)
        self._finalize_alert(
            alert_id,
            ok,
            info,
            f"Proceso finalizado: {target_value}",
            info,
            on_event,
        )

    def _run_battery_worker(
        self,
        alert_id: int,
        threshold_pct: int,
        message: str,
        cancel_event: threading.Event,
        on_event: Callable[[str, str, str], None] | None,
    ) -> None:
        while not cancel_event.is_set():
            bat = psutil.sensors_battery()
            if bat is not None and not bat.power_plugged and bat.percent <= threshold_pct:
                text = (
                    f"🔋 *Alerta de Batería ({bat.percent:.0f}%):*\n"
                    f"{message or f'La batería bajó del {threshold_pct}% y no está conectada.'}"
                )
                ok, info = send_proactive_whatsapp(text)
                self._finalize_alert(
                    alert_id,
                    ok,
                    info,
                    f"Batería <= {threshold_pct}% ({bat.percent:.0f}%)",
                    info,
                    on_event,
                )
                return
            if not self._sleep_interruptible(15, cancel_event):
                return

    def _run_dutic_worker(
        self,
        alert_id: int,
        delay_sec: int,
        message: str,
        cancel_event: threading.Event,
        dutic_fn: Callable[[int], str] | None,
        on_event: Callable[[str, str, str], None] | None,
    ) -> None:
        if delay_sec > 0 and not self._sleep_interruptible(delay_sec, cancel_event):
            return
        if cancel_event.is_set():
            return

        summary = (
            dutic_fn(1)
            if dutic_fn is not None
            else "No se pudo consultar DUTIC en este momento."
        )
        header = "📚 *Briefing Proactivo DUTIC (próximas 24h):*"
        if message:
            header += f"\n_{message}_"
        text = f"{header}\n\n{summary}"
        ok, info = send_proactive_whatsapp(text)
        self._finalize_alert(
            alert_id,
            ok,
            info,
            "Briefing DUTIC 24h enviado",
            info,
            on_event,
        )

    def add_alert(
        self,
        condition_type: str,
        target_value: str,
        message: str = "",
        dutic_fn: Callable[[int], str] | None = None,
        screenshot_fn: Callable[[], str] | None = None,
        on_event: Callable[[str, str, str], None] | None = None,
    ) -> dict[str, Any]:
        """Registra y lanza un vigilante en segundo plano según condition_type."""
        canonical = self._normalize_condition(condition_type)
        clean_target = (target_value or "").strip()
        clean_msg = (message or "").strip()
        cancel_event = threading.Event()

        detail = ""
        worker_target: Callable[..., None]
        worker_args: tuple[Any, ...]

        if canonical == "timer":
            seconds = parse_timer_seconds(clean_target or "5")
            wants_capture = any(
                k in f"{clean_target} {clean_msg}".lower()
                for k in ("captura", "screenshot", "pantalla")
            )
            detail = f"En {seconds}s" + (" (+ captura)" if wants_capture else "")
            worker_target = self._run_timer_worker
            # alert_id se inserta tras crear el registro
            worker_args = (seconds, clean_msg, wants_capture, cancel_event, screenshot_fn, on_event)

        elif canonical == "process_finished":
            if not clean_target:
                raise ValueError("Indica el nombre o PID del proceso a vigilar en target_value.")
            pids = _find_matching_pids(clean_target)
            if not pids:
                raise ValueError(
                    f"No se encontró ningún proceso activo coincidente con '{clean_target}'."
                )
            detail = f"Vigilando {len(pids)} PID(s): {', '.join(map(str, pids[:4]))}"
            worker_target = self._run_process_worker
            worker_args = (clean_target, pids, clean_msg, cancel_event, on_event)

        elif canonical == "battery_low":
            if psutil.sensors_battery() is None:
                raise ValueError("Esta PC no reporta sensor de batería.")
            digits = re.sub(r"\D", "", clean_target)
            threshold = max(5, min(int(digits or "20"), 95))
            detail = f"Umbral <= {threshold}% (desconectado)"
            worker_target = self._run_battery_worker
            worker_args = (threshold, clean_msg, cancel_event, on_event)

        else:  # dutic_check
            delay_sec = 0
            if clean_target and clean_target.lower() not in ("ahora", "now", "24h", "0", "hoy"):
                try:
                    delay_sec = parse_timer_seconds(clean_target)
                except ValueError:
                    delay_sec = 0
            detail = "Revisión inmediata (24h)" if delay_sec == 0 else f"Revisión DUTIC en {delay_sec}s"
            worker_target = self._run_dutic_worker
            worker_args = (delay_sec, clean_msg, cancel_event, dutic_fn, on_event)

        with self._lock:
            self._seq += 1
            alert_id = self._seq
            entry = {
                "id": alert_id,
                "condition_type": canonical,
                "target_value": clean_target or "-",
                "message": clean_msg or "Alerta automática",
                "status": "active",
                "created_at": datetime.now(LIMA_TZ).strftime("%H:%M:%S"),
                "triggered_at": "",
                "detail": detail,
            }
            self._alerts.insert(0, entry)
            self._cancel_flags[alert_id] = cancel_event
            if len(self._alerts) > MAX_ALERTS:
                del self._alerts[MAX_ALERTS:]

        threading.Thread(
            target=worker_target,
            args=(alert_id, *worker_args),
            daemon=True,
        ).start()
        return dict(entry)


watchdog_manager = WatchdogManager()
