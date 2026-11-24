"""Herramientas obligatorias del Vortex AI Desktop."""

from __future__ import annotations

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import ast
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import mss
import mss.tools
import psutil
import pyautogui
pyautogui.FAILSAFE = False
import requests
from google import genai
from google.genai import types
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from icalendar import Calendar
from PIL import Image, ImageChops, ImageGrab, ImageStat

try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError:  # pragma: no cover
    win32api = None  # type: ignore[assignment]
    win32con = None  # type: ignore[assignment]
    win32gui = None  # type: ignore[assignment]
    win32process = None  # type: ignore[assignment]

try:
    import pyperclip
except ImportError:  # pragma: no cover
    pyperclip = None  # type: ignore[assignment]

from agent_core.config import (
    BASE_DIR,
    CREDENTIALS_PATH,
    GEMINI_MODEL,
    GEMINI_MODELS_COMPLEX,
    GEMINI_MODELS_FAST,
    GOOGLE_SCOPES,
    LIMA_TZ,
    TEMP_DIR,
    TOKEN_PATH,
    append_backend_log,
    classify_and_record_model_error,
    clear_model_cooldown,
    filter_available_models,
    get_env,
)
from agent_core.security import (
    assert_screen_privacy,
    classify_terminal_command_risk,
    get_security_context,
    is_secret_path,
    redact_secrets,
    register_action_handler,
    request_confirmation_for_current_task,
    scan_python_ast_for_danger,
    should_require_confirmation,
    wrap_external_data,
)
from agent_core.tools_drive import (
    DRIVE_AND_FILE_TOOLS,
    analyze_pdf_with_gemini,
    download_classroom_attachment,
    list_recent_downloads,
    verify_file,
    verify_file_info,
    wait_for_new_download,
)

# Semáforo global para acceso exclusivo a mouse/teclado/foco GUI en Windows
GUI_LOCK = threading.Lock()
GENERATED_PROJECTS_DIR: Path = BASE_DIR / "generated_projects"
GENERATED_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# Contexto por tarea/worker para aislar imágenes y archivos adjuntos entre ejecuciones paralelas
_CURRENT_TASK_VAR: ContextVar[str] = ContextVar("current_task_id", default="")
_TASK_MAP_LOCK = threading.Lock()
_THREAD_TASK_MAP: dict[int, str] = {}
TASK_STARTED_AT_BY_TASK: dict[str, float] = {}
TASK_CANCEL_EVENTS: dict[str, threading.Event] = {}
TASK_DEADLINE_BY_TASK: dict[str, float] = {}


class TaskCancelledError(RuntimeError):
    """Excepción controlada para abortar cooperativamente una tarea cancelada o expirada."""


def register_task_cancel_event(
    task_id: str,
    event: threading.Event | None = None,
    deadline: float | None = None,
) -> threading.Event:
    """Registra (o actualiza) el `threading.Event` y deadline monotónico de cancelación para `task_id`."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _TASK_MAP_LOCK:
        ev = event or TASK_CANCEL_EVENTS.get(tid) or threading.Event()
        TASK_CANCEL_EVENTS[tid] = ev
        if deadline is not None:
            TASK_DEADLINE_BY_TASK[tid] = float(deadline)
        return ev


def trigger_task_cancel(task_id: str) -> None:
    """Activa el evento de cancelación cooperativa para `task_id`."""
    tid = (task_id or "").strip()
    if not tid:
        return
    with _TASK_MAP_LOCK:
        ev = TASK_CANCEL_EVENTS.get(tid)
        if ev is None:
            ev = threading.Event()
            TASK_CANCEL_EVENTS[tid] = ev
        ev.set()


def clear_task_cancel_event(task_id: str) -> None:
    """Limpia el evento y deadline de cancelación al finalizar la tarea."""
    tid = (task_id or "").strip()
    if not tid:
        return
    with _TASK_MAP_LOCK:
        TASK_CANCEL_EVENTS.pop(tid, None)
        TASK_DEADLINE_BY_TASK.pop(tid, None)


def is_task_cancelled(task_id: str | None = None) -> bool:
    """Indica si la tarea fue cancelada explícitamente o si venció su deadline."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _TASK_MAP_LOCK:
        ev = TASK_CANCEL_EVENTS.get(tid)
        deadline = TASK_DEADLINE_BY_TASK.get(tid)
    if ev is not None and ev.is_set():
        return True
    if deadline is not None and time.monotonic() >= deadline:
        return True
    return False


def _check_not_cancelled(task_id: str | None = None) -> None:
    """Lanza `TaskCancelledError` si la tarea actual fue cancelada o venció."""
    if is_task_cancelled(task_id):
        raise TaskCancelledError("Operación cancelada por timeout o señal de cancelación de tarea.")


def _kill_process_tree(pid: int) -> None:
    """Mata un proceso y todo su árbol de hijos en Windows (`taskkill /F /T /PID`)."""
    if pid <= 0:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        pass

# Colas thread-safe aisladas por task_id para imágenes y archivos generados por cada worker
_IMAGES_LOCK = threading.Lock()
PENDING_IMAGES: list[str] = []
PENDING_IMAGES_BY_TASK: dict[str, list[str]] = {}
LAST_INTERNAL_SCREENSHOT_BY_TASK: dict[str, str] = {}

_FILES_LOCK = threading.Lock()
PENDING_FILES: list[str] = []
PENDING_FILES_BY_TASK: dict[str, list[str]] = {}

# Modo de monitor predeterminado: 0 = Auto (ventana activa), 1..3 = Monitor N, -1 = Panorámica
_MONITOR_LOCK = threading.Lock()
DEFAULT_MONITOR_MODE: int = 0


def set_current_task_context(task_id: str, started_at: float | None = None) -> None:
    """Asocia el hilo y contexto actual a un task_id único de worker y registra su inicio."""
    tid = (task_id or "").strip() or f"thread-{threading.get_ident()}"
    _CURRENT_TASK_VAR.set(tid)
    with _TASK_MAP_LOCK:
        _THREAD_TASK_MAP[threading.get_ident()] = tid
        if started_at is not None:
            TASK_STARTED_AT_BY_TASK[tid] = float(started_at)
        elif tid not in TASK_STARTED_AT_BY_TASK:
            TASK_STARTED_AT_BY_TASK[tid] = time.time()


def clear_current_task_context(task_id: str | None = None) -> None:
    """Limpia la asociación del hilo actual al finalizar una tarea (y TASK_STARTED_AT si se pasa task_id)."""
    _CURRENT_TASK_VAR.set("")
    with _TASK_MAP_LOCK:
        _THREAD_TASK_MAP.pop(threading.get_ident(), None)
        if task_id and task_id.strip():
            TASK_STARTED_AT_BY_TASK.pop(task_id.strip(), None)


def get_task_started_at(task_id: str | None = None) -> float | None:
    """Obtiene el timestamp Unix de inicio de la tarea actual, o None si no hay tarea activa."""
    tid = (task_id or "").strip() or _CURRENT_TASK_VAR.get()
    with _TASK_MAP_LOCK:
        if not tid:
            tid = _THREAD_TASK_MAP.get(threading.get_ident(), "")
        if not tid:
            return None
        return TASK_STARTED_AT_BY_TASK.get(tid)


def get_current_task_id() -> str:
    """Obtiene el task_id asociado al contexto/hilo actual."""
    ctx_id = _CURRENT_TASK_VAR.get()
    if ctx_id:
        return ctx_id
    with _TASK_MAP_LOCK:
        mapped = _THREAD_TASK_MAP.get(threading.get_ident())
        if mapped:
            return mapped
    return f"thread-{threading.get_ident()}"


def pop_pending_images(task_id: str | None = None) -> list[str]:
    """Extrae y limpia únicamente las imágenes pendientes asociadas al task_id actual."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _IMAGES_LOCK:
        images = list(PENDING_IMAGES_BY_TASK.pop(tid, []))
        if images:
            remove_set = set(images)
            PENDING_IMAGES[:] = [p for p in PENDING_IMAGES if p not in remove_set]
        return images


def _enqueue_image(path: Path, task_id: str | None = None) -> None:
    """Agrega la ruta absoluta de una imagen a la cola aislada del task_id actual."""
    tid = (task_id or "").strip() or get_current_task_id()
    resolved = str(path.resolve())
    with _IMAGES_LOCK:
        PENDING_IMAGES_BY_TASK.setdefault(tid, []).append(resolved)
        PENDING_IMAGES.append(resolved)


def record_internal_screenshot(path: Path | str, task_id: str | None = None) -> str:
    """Registra la última captura interna tomada en el turno para que el agente la vea."""
    tid = (task_id or "").strip() or get_current_task_id()
    resolved = str(Path(path).resolve())
    with _IMAGES_LOCK:
        LAST_INTERNAL_SCREENSHOT_BY_TASK[tid] = resolved
    return resolved


def get_last_internal_screenshot(task_id: str | None = None) -> str | None:
    """Obtiene sin consumir la ruta de la última captura interna de la tarea."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _IMAGES_LOCK:
        return LAST_INTERNAL_SCREENSHOT_BY_TASK.get(tid)


def pop_last_internal_screenshot(task_id: str | None = None) -> str | None:
    """Extrae y limpia el puntero a la última captura interna de la tarea."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _IMAGES_LOCK:
        return LAST_INTERNAL_SCREENSHOT_BY_TASK.pop(tid, None)


def clear_last_internal_screenshot(task_id: str | None = None) -> None:
    """Limpia el estado de captura interna al finalizar una tarea."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _IMAGES_LOCK:
        LAST_INTERNAL_SCREENSHOT_BY_TASK.pop(tid, None)


def pop_pending_files(task_id: str | None = None) -> list[str]:
    """Extrae y limpia únicamente los documentos pendientes asociados al task_id actual."""
    tid = (task_id or "").strip() or get_current_task_id()
    with _FILES_LOCK:
        files = list(PENDING_FILES_BY_TASK.pop(tid, []))
        if files:
            remove_set = set(files)
            PENDING_FILES[:] = [p for p in PENDING_FILES if p not in remove_set]
        return files


def _enqueue_file(path: Path, task_id: str | None = None) -> None:
    """Agrega la ruta absoluta de un archivo a la cola aislada del task_id actual."""
    tid = (task_id or "").strip() or get_current_task_id()
    resolved = str(path.resolve())
    with _FILES_LOCK:
        PENDING_FILES_BY_TASK.setdefault(tid, []).append(resolved)
        PENDING_FILES.append(resolved)


def set_default_monitor_mode(mode: int) -> int:
    """Establece el modo de monitor por defecto (0=Auto, 1..N=Monitor, -1=Panorámica)."""
    global DEFAULT_MONITOR_MODE
    with _MONITOR_LOCK:
        DEFAULT_MONITOR_MODE = int(mode)
        return DEFAULT_MONITOR_MODE


def get_default_monitor_mode() -> int:
    """Obtiene el modo de monitor configurado actualmente."""
    with _MONITOR_LOCK:
        return DEFAULT_MONITOR_MODE


def _get_monitors_dynamic() -> list[dict[str, Any]]:
    """Lista dinámicamente los monitores físicos en tiempo real mediante mss (Per-Monitor DPI Aware)."""
    role_names = {
        1: "Laptop / Principal",
        2: "TV / Externo",
        3: "Externo 2",
    }
    try:
        mss_factory = getattr(mss, "MSS", mss.mss)
        with mss_factory() as sct:
            monitors: list[dict[str, Any]] = []
            for idx, mon in enumerate(sct.monitors[1:], start=1):
                left = int(mon.get("left", 0))
                top = int(mon.get("top", 0))
                width = int(mon.get("width", 0))
                height = int(mon.get("height", 0))
                role = role_names.get(idx, "Externo")
                monitors.append(
                    {
                        "id": idx,
                        "index": idx,
                        "name": f"Monitor {idx} ({role} {width}x{height})",
                        "left": left,
                        "top": top,
                        "width": width,
                        "height": height,
                        "is_primary": left == 0 and top == 0,
                        "mss_rect": {
                            "left": left,
                            "top": top,
                            "width": width,
                            "height": height,
                        },
                    }
                )
            return monitors
    except Exception:
        return []


def get_monitors_metadata() -> list[dict[str, Any]]:
    """Alias compatible para listar dinámicamente los monitores físicos detectados."""
    return _get_monitors_dynamic()


def _to_lima_datetime(value: Any, end_of_day_if_date: bool = False) -> datetime | None:
    """Convierte un date/datetime (naive o con tz) a datetime consciente en America/Lima."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=LIMA_TZ)
        return value.astimezone(LIMA_TZ)
    if isinstance(value, date):
        t = dt_time(23, 59, 59) if end_of_day_if_date else dt_time(0, 0, 0)
        return datetime.combine(value, t, tzinfo=LIMA_TZ)
    return None


def _parse_iso_lima(iso_str: str) -> datetime:
    """Parsea un string ISO-8601 y garantiza zona horaria America/Lima."""
    cleaned = iso_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LIMA_TZ)
    return dt.astimezone(LIMA_TZ)


def _get_google_creds() -> tuple[Credentials | None, str | None]:
    """Carga y refresca credenciales OAuth2 desde token.json."""
    if not TOKEN_PATH.exists():
        if not CREDENTIALS_PATH.exists():
            return (
                None,
                "Falta configurar Google OAuth2: coloca 'credentials.json' en la raíz "
                "del proyecto y ejecuta `python setup_google_auth.py`.",
            )
        return (
            None,
            "Aún no has iniciado sesión con Google. Ejecuta `python setup_google_auth.py` "
            "para generar `token.json`.",
        )

    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH))
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        if not creds or not creds.valid:
            return (
                None,
                "El token de Google no es válido o expiró. Vuelve a ejecutar `python setup_google_auth.py`.",
            )
        return creds, None
    except Exception as exc:
        return None, f"Error al cargar credenciales de Google: {exc}"


# ============================================================================
# 1. UNIVERSIDAD - DUTIC UNSA (Moodle .ics)
# ============================================================================


def get_dutic_events(days_ahead: int = 7) -> str:
    """Obtiene tareas, cuestionarios y entregas próximas desde el calendario iCal (.ics) de DUTIC UNSA.

    Args:
        days_ahead: Número de días hacia adelante a consultar (por defecto 7).

    Returns:
        Lista formateada de eventos y entregas en zona horaria America/Lima (UTC-5).
    """
    url = get_env("DUTIC_ICAL_URL")
    if not url:
        return (
            "DUTIC_ICAL_URL no está configurado en el archivo .env. "
            "Ve al Aula Virtual DUTIC -> Calendario -> Exportar calendario -> "
            "Todos los eventos -> Eventos próximos -> Obtener URL del calendario, "
            "y pégala en DUTIC_ICAL_URL dentro de .env."
        )

    days = max(1, min(int(days_ahead), 90))
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
    except Exception as exc:
        return f"Error al descargar o parsear el calendario DUTIC (.ics): {exc}"

    now_lima = datetime.now(LIMA_TZ)
    window_start = now_lima - timedelta(hours=12)
    window_end = now_lima + timedelta(days=days)

    events: list[tuple[datetime, str, str, str]] = []
    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        dtstart_prop = component.get("DTSTART")
        if not dtstart_prop:
            continue

        start_dt = _to_lima_datetime(dtstart_prop.dt)
        if not start_dt:
            continue

        if not (window_start <= start_dt <= window_end):
            continue

        summary = str(component.get("SUMMARY", "Sin título")).strip()
        categories = component.get("CATEGORIES")
        if isinstance(categories, list):
            cat_str = ", ".join(str(c.to_ical().decode("utf-8", errors="ignore") if hasattr(c, "to_ical") else c) for c in categories)
        elif categories is not None:
            cat_str = str(categories.to_ical().decode("utf-8", errors="ignore") if hasattr(categories, "to_ical") else categories)
        else:
            cat_str = ""

        description = str(component.get("DESCRIPTION", "")).strip()
        if len(description) > 180:
            description = description[:177].rstrip() + "..."

        events.append((start_dt, summary, cat_str, description))

    events.sort(key=lambda item: item[0])

    if not events:
        return (
            f"No hay eventos o entregas en DUTIC entre hoy y los próximos {days} días "
            f"(hora Lima: {now_lima.strftime('%Y-%m-%d %H:%M')})."
        )

    lines = [
        f"Eventos DUTIC UNSA (próximos {days} días | Zona horaria America/Lima):"
    ]
    for start_dt, summary, cat_str, desc in events:
        estado = "HOY/VENCE PRONTO" if start_dt.date() == now_lima.date() else "Próximo"
        if start_dt < now_lima:
            estado = "Reciente (<12h)"
        fecha_fmt = start_dt.strftime("%Y-%m-%d %H:%M")
        curso_tag = f" [{cat_str}]" if cat_str else ""
        lines.append(f"- [{estado}] {fecha_fmt}{curso_tag}: {summary}")
        if desc:
            lines.append(f"  Detalle: {desc.replace(chr(10), ' ')}")

    return wrap_external_data("\n".join(lines))


# ============================================================================
# 2. UNIVERSIDAD - GOOGLE CLASSROOM Y GOOGLE CALENDAR (OAuth2)
# ============================================================================


def _classroom_due_to_lima(work: dict[str, Any]) -> datetime | None:
    """Convierte dueDate y dueTime (UTC en Classroom API) a America/Lima."""
    due_date = work.get("dueDate")
    if not due_date:
        return None

    year = int(due_date.get("year", 1970))
    month = int(due_date.get("month", 1))
    day = int(due_date.get("day", 1))
    due_time = work.get("dueTime") or {}
    hour = int(due_time.get("hours", 23))
    minute = int(due_time.get("minutes", 59))
    second = int(due_time.get("seconds", 0))

    utc_dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return utc_dt.astimezone(LIMA_TZ)


def get_classroom_pending() -> str:
    """Lista cursos activos en Google Classroom con tareas pendientes y anuncios recientes.

    Returns:
        Resumen estructurado por curso con fechas límite en America/Lima.
    """
    creds, err = _get_google_creds()
    if err or creds is None:
        return err or "No se pudo autenticar con Google."

    try:
        service = build("classroom", "v1", credentials=creds, cache_discovery=False)
        courses_resp = service.courses().list(courseStates=["ACTIVE"], pageSize=30).execute()
        courses = courses_resp.get("courses", [])
    except Exception as exc:
        return f"Error al consultar cursos en Google Classroom: {exc}"

    if not courses:
        return "No tienes cursos activos en Google Classroom."

    now_lima = datetime.now(LIMA_TZ)
    cutoff_overdue = now_lima - timedelta(days=3)
    cutoff_recent_created = now_lima - timedelta(days=14)
    cutoff_announcements = now_lima - timedelta(days=7)

    output_sections: list[str] = [
        f"Resumen Google Classroom (hora Lima: {now_lima.strftime('%Y-%m-%d %H:%M')}):"
    ]

    for course in courses:
        course_id = course.get("id", "")
        course_name = course.get("name", "Curso sin nombre")
        pending_items: list[str] = []
        recent_announcements: list[str] = []

        # Obtener tareas del curso
        try:
            cw_resp = (
                service.courses()
                .courseWork()
                .list(courseId=course_id, pageSize=25, orderBy="updateTime desc")
                .execute()
            )
            course_works = cw_resp.get("courseWork", [])
        except Exception:
            course_works = []

        for work in course_works:
            work_id = work.get("id", "")
            title = work.get("title", "Tarea sin título")
            due_lima = _classroom_due_to_lima(work)

            # Verificar si vence pronto, es reciente o venció hace <= 3 días
            is_relevant = False
            if due_lima is not None:
                if due_lima >= cutoff_overdue:
                    is_relevant = True
            else:
                creation_str = work.get("creationTime")
                if creation_str:
                    created_dt = _parse_iso_lima(creation_str)
                    if created_dt >= cutoff_recent_created:
                        is_relevant = True

            if not is_relevant:
                continue

            # Revisar estado de entrega del estudiante
            submission_state = "UNKNOWN"
            try:
                sub_resp = (
                    service.courses()
                    .courseWork()
                    .studentSubmissions()
                    .list(courseId=course_id, courseWorkId=work_id, userId="me")
                    .execute()
                )
                subs = sub_resp.get("studentSubmissions", [])
                if subs:
                    submission_state = subs[0].get("state", "UNKNOWN")
            except Exception:
                pass

            if submission_state in {"TURNED_IN", "RETURNED"}:
                continue

            if due_lima:
                status_tag = "VENCIDA RECIENTE" if due_lima < now_lima else "Pendiente"
                due_str = due_lima.strftime("%Y-%m-%d %H:%M")
            else:
                status_tag = "Sin fecha límite"
                due_str = "Sin fecha"

            link = work.get("alternateLink", "")
            line = f"  * [{status_tag}] {title} | Entrega: {due_str} | Estado: {submission_state}"
            if link:
                line += f" | {link}"
            pending_items.append(line)

        # Obtener últimos 2 anuncios recientes (últimos 7 días)
        try:
            ann_resp = (
                service.courses()
                .announcements()
                .list(courseId=course_id, pageSize=5, orderBy="updateTime desc")
                .execute()
            )
            announcements = ann_resp.get("announcements", [])
            for ann in announcements:
                upd_str = ann.get("updateTime") or ann.get("creationTime")
                if upd_str:
                    upd_dt = _parse_iso_lima(upd_str)
                    if upd_dt < cutoff_announcements:
                        continue
                    fecha_ann = upd_dt.strftime("%Y-%m-%d %H:%M")
                else:
                    fecha_ann = "Reciente"
                text = str(ann.get("text", "")).strip().replace("\n", " ")
                if len(text) > 160:
                    text = text[:157].rstrip() + "..."
                recent_announcements.append(f"  * Anuncio ({fecha_ann}): {wrap_external_data(text)}")
                if len(recent_announcements) >= 2:
                    break
        except Exception:
            pass

        if pending_items or recent_announcements:
            output_sections.append(f"\n[{course_name}]")
            output_sections.extend(pending_items)
            output_sections.extend(recent_announcements)

    if len(output_sections) == 1:
        return (
            f"Revisé tus {len(courses)} cursos activos de Google Classroom y no tienes "
            "tareas pendientes próximas ni anuncios nuevos en los últimos 7 días."
        )

    return "\n".join(output_sections)


def _compute_free_slots(
    target_date: date,
    busy_intervals: list[tuple[datetime, datetime]],
) -> list[str]:
    """Calcula huecos libres entre 07:00 y 22:00 en America/Lima."""
    day_start = datetime.combine(target_date, dt_time(7, 0), tzinfo=LIMA_TZ)
    day_end = datetime.combine(target_date, dt_time(22, 0), tzinfo=LIMA_TZ)

    clipped: list[tuple[datetime, datetime]] = []
    for start_dt, end_dt in busy_intervals:
        s = max(start_dt, day_start)
        e = min(end_dt, day_end)
        if s < e:
            clipped.append((s, e))

    clipped.sort(key=lambda pair: pair[0])
    merged: list[tuple[datetime, datetime]] = []
    for s, e in clipped:
        if not merged or s > merged[-1][1]:
            merged.append((s, e))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))

    free_slots: list[str] = []
    cursor = day_start
    for s, e in merged:
        if s - cursor >= timedelta(minutes=15):
            free_slots.append(f"{cursor.strftime('%H:%M')} - {s.strftime('%H:%M')}")
        cursor = max(cursor, e)

    if day_end - cursor >= timedelta(minutes=15):
        free_slots.append(f"{cursor.strftime('%H:%M')} - {day_end.strftime('%H:%M')}")

    return free_slots


def get_calendar_schedule(date_iso: str = "") -> str:
    """Lee los eventos de Google Calendar para una fecha (America/Lima) y calcula huecos libres.

    Args:
        date_iso: Fecha en formato YYYY-MM-DD o ISO-8601. Si se omite, usa la fecha de hoy en Lima.

    Returns:
        Eventos del día y bloques libres disponibles entre las 07:00 y 22:00.
    """
    creds, err = _get_google_creds()
    if err or creds is None:
        return err or "No se pudo autenticar con Google."

    try:
        if date_iso.strip():
            raw = date_iso.strip()
            target_date = (
                date.fromisoformat(raw)
                if len(raw) == 10
                else _parse_iso_lima(raw).date()
            )
        else:
            target_date = datetime.now(LIMA_TZ).date()
    except ValueError as exc:
        return f"Formato de fecha inválido '{date_iso}'. Usa YYYY-MM-DD. Detalle: {exc}"

    time_min = datetime.combine(target_date, dt_time(0, 0, 0), tzinfo=LIMA_TZ)
    time_max = datetime.combine(target_date, dt_time(23, 59, 59), tzinfo=LIMA_TZ)
    calendar_id = get_env("GOOGLE_CALENDAR_ID", "primary") or "primary"

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        events_resp = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_resp.get("items", [])
    except Exception as exc:
        return f"Error al leer Google Calendar: {exc}"

    busy_intervals: list[tuple[datetime, datetime]] = []
    lines = [f"Agenda Google Calendar para {target_date.isoformat()} (America/Lima):"]

    if not events:
        lines.append("- No hay eventos programados este día.")
    else:
        for ev in events:
            summary = ev.get("summary", "Sin título")
            start_info = ev.get("start", {})
            end_info = ev.get("end", {})
            location = ev.get("location", "")

            if "dateTime" in start_info and "dateTime" in end_info:
                s_dt = _parse_iso_lima(start_info["dateTime"])
                e_dt = _parse_iso_lima(end_info["dateTime"])
                busy_intervals.append((s_dt, e_dt))
                time_label = f"{s_dt.strftime('%H:%M')} - {e_dt.strftime('%H:%M')}"
            else:
                time_label = "Todo el día"

            loc_str = f" | Lugar: {location}" if location else ""
            lines.append(f"- {time_label}: {summary}{loc_str}")

    free_slots = _compute_free_slots(target_date, busy_intervals)
    lines.append("\nHuecos libres (entre 07:00 y 22:00):")
    if free_slots:
        for slot in free_slots:
            lines.append(f"- {slot}")
    else:
        lines.append("- Día completamente ocupado entre 07:00 y 22:00.")

    return "\n".join(lines)


def create_calendar_event(
    title: str,
    start_iso: str,
    end_iso: str,
    description: str = "",
) -> str:
    """Agenda un bloque de estudio, clase o recordatorio en Google Calendar con alarmas a los 15 y 30 min.

    Args:
        title: Título del evento.
        start_iso: Fecha y hora de inicio en ISO-8601 (ej. '2026-11-15T16:00:00' o con offset -05:00).
        end_iso: Fecha y hora de fin en ISO-8601 (ej. '2026-11-15T18:00:00').
        description: Descripción opcional del evento.

    Returns:
        Confirmación con el horario agendado y enlace al evento.
    """
    creds, err = _get_google_creds()
    if err or creds is None:
        return err or "No se pudo autenticar con Google."

    try:
        start_dt = _parse_iso_lima(start_iso)
        end_dt = _parse_iso_lima(end_iso)
        if end_dt <= start_dt:
            return "La hora de fin debe ser posterior a la hora de inicio."
    except ValueError as exc:
        return f"Formato de fecha/hora inválido (usa ISO-8601 como YYYY-MM-DDTHH:MM:SS): {exc}"

    calendar_id = get_env("GOOGLE_CALENDAR_ID", "primary") or "primary"
    event_body = {
        "summary": title,
        "description": description,
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": "America/Lima",
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": "America/Lima",
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 15},
                {"method": "popup", "minutes": 30},
            ],
        },
    }

    try:
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        created = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        link = created.get("htmlLink", "")
        return (
            f"Evento agendado correctamente en Google Calendar:\n"
            f"- Título: {title}\n"
            f"- Inicio: {start_dt.strftime('%Y-%m-%d %H:%M')} (America/Lima)\n"
            f"- Fin: {end_dt.strftime('%Y-%m-%d %H:%M')} (America/Lima)\n"
            f"- Recordatorios: Popup 15 min y 30 min antes\n"
            f"- Enlace: {link}"
        )
    except Exception as exc:
        return f"Error al crear el evento en Google Calendar: {exc}"


# ============================================================================
# 3. CONTROL TOTAL DE PC (WINDOWS) Y MULTI-MONITOR INTELIGENTE
# ============================================================================


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _get_window_title(hwnd: int) -> str:
    """Obtiene el título de una ventana Win32 a partir de su handle."""
    if not hwnd:
        return ""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _find_window_handle(keywords: list[str]) -> tuple[int, str]:
    """Busca el primer handle de ventana visible cuyo título coincida con alguna palabra clave."""
    user32 = ctypes.windll.user32
    matches: list[tuple[int, str]] = []
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    lowered_kws = [k.lower() for k in keywords if k and k.strip()]
    if not lowered_kws:
        return 0, ""

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_title(hwnd)
        if not title:
            return True
        title_lower = title.lower()
        if any(kw in title_lower for kw in lowered_kws):
            matches.append((hwnd, title))
        return True

    user32.EnumWindows(EnumWindowsProc(_callback), 0)
    return matches[0] if matches else (0, "")


def _get_window_center(hwnd: int) -> tuple[int, int] | None:
    """Devuelve el centro (cx, cy) en coordenadas físicas reales usando win32gui.GetWindowRect."""
    if not hwnd:
        return None
    if win32gui is not None:
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right > left and bottom > top:
                return (int(left) + int(right)) // 2, (int(top) + int(bottom)) // 2
        except Exception:
            pass
    user32 = ctypes.windll.user32
    rect = _RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2


def _detect_monitor_for_window(
    monitors: list[dict[str, Any]],
    target_window_title: str = "",
) -> tuple[int, str]:
    """Determina qué monitor físico (1..N) contiene el centro (cx, cy) de la ventana objetivo/activa."""
    physical_count = max(0, len(monitors) - 1)
    if physical_count <= 0:
        return 0, ""

    hwnd = 0
    win_title = ""
    if target_window_title and target_window_title.strip():
        hwnd, win_title = _find_window_handle([target_window_title.strip()])
    if not hwnd:
        user32 = ctypes.windll.user32
        hwnd = int(user32.GetForegroundWindow() or 0)
        win_title = _get_window_title(hwnd)

    center = _get_window_center(hwnd)
    if center is not None:
        cx, cy = center
        for idx, mon in enumerate(monitors[1:], start=1):
            left = int(mon.get("left", 0))
            top = int(mon.get("top", 0))
            width = int(mon.get("width", 0))
            height = int(mon.get("height", 0))
            if left <= cx < left + width and top <= cy < top + height:
                return idx, win_title

        # Si el centro cae justo fuera de borde, elegir el monitor físico más cercano
        best_idx = 1
        best_dist = float("inf")
        for idx, mon in enumerate(monitors[1:], start=1):
            mcx = int(mon.get("left", 0)) + int(mon.get("width", 0)) / 2.0
            mcy = int(mon.get("top", 0)) + int(mon.get("height", 0)) / 2.0
            dist = math.hypot(cx - mcx, cy - mcy)
            if dist < best_dist:
                best_dist = dist
                best_idx = idx
        return best_idx, win_title

    return 1, win_title


def capture_monitor_image(
    monitor_number: int = 0,
    target_window_title: str = "",
    enqueue: bool = False,
) -> dict[str, Any]:
    """Captura un monitor específico o el monitor de la ventana activa usando sus dimensiones físicas exactas.

    Args:
        monitor_number: 0=Auto (monitor de la ventana), 1..3=Monitor físico, -1=Panorámica total.
        target_window_title: Título parcial de la ventana a localizar cuando monitor_number == 0.
        enqueue: Si True, encola la imagen resultante para enviarse por WhatsApp.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    requested_mode = int(monitor_number)
    if requested_mode == 0 and not (target_window_title or "").strip():
        configured_mode = get_default_monitor_mode()
        if configured_mode != 0:
            requested_mode = configured_mode

    detected_window = ""
    target_idx = 0
    total_physical = 0
    width = 0
    height = 0
    label = "Monitor Principal"

    mon_rect: dict[str, int] | None = None
    try:
        mss_factory = getattr(mss, "MSS", mss.mss)
        with mss_factory() as sct:
            total_physical = max(0, len(sct.monitors) - 1)
            if requested_mode == -1:
                target_idx = 0
                label = f"Panorámica Completa ({max(1, total_physical)} monitores)"
            elif requested_mode >= 1:
                if total_physical >= 1:
                    target_idx = min(requested_mode, total_physical)
                    label = f"Monitor {target_idx} de {total_physical}"
                else:
                    target_idx = 0
                    label = "Monitor Principal"
            else:
                target_idx, detected_window = _detect_monitor_for_window(
                    sct.monitors, target_window_title
                )
                if target_idx >= 1:
                    win_desc = detected_window or target_window_title or "Ventana activa"
                    if len(win_desc) > 42:
                        win_desc = win_desc[:39].rstrip() + "..."
                    label = f"Monitor {target_idx} de {total_physical} (Auto: {win_desc})"
                else:
                    label = "Monitor Principal"

            raw_mon = sct.monitors[target_idx]
            mon_rect = {
                "left": int(raw_mon.get("left", 0)),
                "top": int(raw_mon.get("top", 0)),
                "width": int(raw_mon.get("width", 0)),
                "height": int(raw_mon.get("height", 0)),
            }
            width = mon_rect["width"]
            height = mon_rect["height"]
            out_path = TEMP_DIR / f"screenshot_m{target_idx}_{int(time.time() * 1000)}.png"
            sct_img = sct.grab(mon_rect)
            mss.tools.to_png(sct_img.rgb, sct_img.size, output=str(out_path))
    except Exception as mss_exc:
        out_path = TEMP_DIR / f"screenshot_fallback_{int(time.time() * 1000)}.png"
        pil_exc: Exception | None = None
        captured = False

        # Intento 2: PIL ImageGrab (con bbox del monitor si está disponible o escritorio completo)
        try:
            bbox = None
            if mon_rect and all(k in mon_rect for k in ("left", "top", "width", "height")):
                l = int(mon_rect["left"])
                t = int(mon_rect["top"])
                bbox = (l, t, l + int(mon_rect["width"]), t + int(mon_rect["height"]))
            try:
                img = ImageGrab.grab(bbox=bbox, all_screens=True)
            except Exception:
                img = ImageGrab.grab()
            width, height = img.size
            img.save(str(out_path), format="PNG")
            label = "Panorámica (ImageGrab)" if requested_mode == -1 else "Escritorio (ImageGrab)"
            captured = True
        except Exception as exc2:
            pil_exc = exc2

        # Intento 3: pyautogui.screenshot()
        if not captured:
            try:
                img = pyautogui.screenshot()
                width, height = img.size
                img.save(str(out_path), format="PNG")
                label = "Escritorio (PyAutoGUI Fallback)"
                captured = True
            except Exception as pyauto_exc:
                return {
                    "ok": False,
                    "error": (
                        "No se pudo tomar la captura de pantalla en ningún modo "
                        f"(mss={mss_exc}; pil={pil_exc}; pyautogui={pyauto_exc})."
                    ),
                }

    record_internal_screenshot(out_path)
    if enqueue:
        _enqueue_image(out_path)

    return {
        "ok": True,
        "path": str(out_path),
        "monitor_index": target_idx,
        "total_monitors": total_physical,
        "width": width,
        "height": height,
        "label": label,
        "detected_window": detected_window,
        "monitor": mon_rect or {"left": 0, "top": 0, "width": width, "height": height},
    }


def take_screenshot(
    monitor_number: int = 0,
    target_window_title: str = "",
    send_to_whatsapp: bool = False,
) -> str:
    """Toma una captura de pantalla inteligente (3 monitores) para visión interna o envío al usuario.

    Args:
        monitor_number:
            0 = Automático: detecta el monitor donde está `target_window_title` o la ventana activa
                (o respeta el monitor configurado desde el Dashboard).
            1, 2 o 3 = Fuerza la captura del monitor físico específico (laptop, secundario, TV, etc.).
            -1 = Captura panorámica unificada de todos los monitores al mismo tiempo.
        target_window_title: Título parcial opcional de una ventana (ej. 'Brave', 'Visual Studio Code', 'Antigravity')
            para detectar y capturar exactamente el monitor donde se encuentra esa app.
        send_to_whatsapp: Si True, encola la imagen para enviarla al chat del usuario. Usa True
            cuando el usuario pida explícitamente una captura o como entrega visual final; déjalo en
            False (por defecto) para inspecciones internas intermedias.

    Returns:
        Confirmación con el monitor detectado, resolución y ruta del archivo PNG generado.
    """
    result = capture_monitor_image(
        monitor_number=monitor_number,
        target_window_title=target_window_title,
        enqueue=bool(send_to_whatsapp),
    )
    if not result.get("ok"):
        return str(result.get("error", "Error desconocido al capturar pantalla."))

    delivery_note = (
        "y encolada para enviarse por WhatsApp."
        if send_to_whatsapp
        else "(solo captura interna de visión; no enviada a WhatsApp)."
    )
    return (
        f"Captura guardada ({result['label']} | {result['width']}x{result['height']}) "
        f"en '{result['path']}' {delivery_note}"
    )


def get_system_status() -> str:
    """Obtiene el estado actual de la PC: CPU, RAM, disco, batería y Top 5 procesos por consumo.

    Returns:
        Reporte detallado del sistema Windows.
    """
    cpu_pct = psutil.cpu_percent(interval=0.5)
    vm = psutil.virtual_memory()
    ram_used_gb = vm.used / (1024**3)
    ram_total_gb = vm.total / (1024**3)

    try:
        disk = psutil.disk_usage("C:\\")
        disk_line = (
            f"Disco C:: {disk.used / (1024**3):.1f} GB / "
            f"{disk.total / (1024**3):.1f} GB ({disk.percent}%)"
        )
    except Exception:
        disk_line = "Disco C:: No disponible"

    battery = psutil.sensors_battery()
    if battery is not None:
        plugged = "Conectado a corriente" if battery.power_plugged else "Usando batería"
        secs = battery.secsleft
        time_left = (
            f" (~{secs // 3600}h {(secs % 3600) // 60}m restantes)"
            if secs not in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) and secs >= 0
            else ""
        )
        battery_line = f"Batería: {battery.percent:.0f}% ({plugged}{time_left})"
    else:
        battery_line = "Batería: PC de escritorio o sensor no disponible"

    procs: list[tuple[float, float, str, int]] = []
    for proc in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
        try:
            info = proc.info
            pid = info.get("pid", 0)
            if pid == 0:
                continue
            name = info.get("name") or "desconocido"
            mem_pct = float(info.get("memory_percent") or 0.0)
            cpu_p = float(info.get("cpu_percent") or 0.0)
            procs.append((mem_pct, cpu_p, name, pid))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    top_ram = sorted(procs, key=lambda x: x[0], reverse=True)[:5]

    lines = [
        "Estado actual de la PC (Windows):",
        f"- CPU Total: {cpu_pct:.1f}%",
        f"- RAM: {ram_used_gb:.2f} GB / {ram_total_gb:.2f} GB ({vm.percent:.1f}%)",
        f"- {disk_line}",
        f"- {battery_line}",
        "\nTop 5 procesos por uso de RAM:",
    ]
    for mem_pct, cpu_p, name, pid in top_ram:
        mem_mb = (mem_pct / 100.0) * (vm.total / (1024**2))
        lines.append(f"- {name} (PID {pid}): {mem_mb:.0f} MB RAM ({mem_pct:.1f}%) | CPU: {cpu_p:.1f}%")

    return "\n".join(lines)


def run_terminal_command(command: str) -> str:
    """Ejecuta un comando en PowerShell de Windows con timeout de seguridad de 30 segundos.

    Args:
        command: Comando PowerShell a ejecutar.

    Returns:
        Código de salida, stdout y stderr del comando.
    """
    if not command.strip():
        return "El comando no puede estar vacío."

    risk_reason = classify_terminal_command_risk(command)
    if risk_reason and should_require_confirmation():
        return request_confirmation_for_current_task(
            action_name="run_terminal_command",
            args_dict={"command": command},
            reason=risk_reason,
            task_id=get_current_task_id(),
        )

    sender_ctx, _, _ = get_security_context()
    proc: subprocess.Popen[str] | None = None
    deadline = time.monotonic() + 30.0
    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        while proc.poll() is None:
            if is_task_cancelled():
                _kill_process_tree(proc.pid)
                try:
                    proc.kill()
                except Exception:
                    pass
                append_backend_log(
                    f"[Terminal] sender={sender_ctx} | exit=CANCELLED | cmd={redact_secrets(command)}"
                )
                return "Error: Comando cancelado por timeout o cancelación de tarea (árbol de procesos terminado)."
            if time.monotonic() >= deadline:
                _kill_process_tree(proc.pid)
                try:
                    proc.kill()
                except Exception:
                    pass
                append_backend_log(
                    f"[Terminal] sender={sender_ctx} | exit=TIMEOUT | cmd={redact_secrets(command)}"
                )
                return "Error: El comando excedió el límite de seguridad de 30 segundos y fue cancelado."
            time.sleep(0.2)

        stdout_raw, stderr_raw = proc.communicate(timeout=2)
        returncode = int(proc.returncode or 0)
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)
            try:
                proc.kill()
            except Exception:
                pass
        append_backend_log(
            f"[Terminal] sender={sender_ctx} | exit=ERR | cmd={redact_secrets(command)}"
        )
        return f"Error al ejecutar comando en PowerShell: {exc}"

    append_backend_log(
        f"[Terminal] sender={sender_ctx} | exit={returncode} | cmd={redact_secrets(command)}"
    )

    stdout = (stdout_raw or "").strip()
    stderr = (stderr_raw or "").strip()

    if len(stdout) > 3500:
        stdout = stdout[:3500] + "\n...[salida truncada]..."
    if len(stderr) > 1500:
        stderr = stderr[:1500] + "\n...[stderr truncado]..."

    parts = [f"Código de salida: {returncode}"]
    if stdout:
        parts.append(f"STDOUT:\n{stdout}")
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(Comando ejecutado sin salida en consola)")

    return "\n".join(parts)


def open_application_or_url(target: str) -> str:
    """Abre una URL en el navegador predeterminado o inicia una aplicación/archivo en Windows.

    Args:
        target: URL (http/https), nombre de aplicación (ej. 'notepad', 'calc', 'code') o ruta local.

    Returns:
        Confirmación de apertura o detalle del error.
    """
    clean = target.strip()
    if not clean:
        return "Debes indicar una URL o nombre/ruta de aplicación."

    if clean.lower().startswith(("http://", "https://")):
        try:
            os.startfile(clean)
        except Exception:
            webbrowser.open(clean)
        return f"URL abierta en el navegador: {clean}"

    try:
        os.startfile(clean)
        return f"Aplicación o archivo abierto: {clean}"
    except Exception:
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", clean],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return f"Se envió la orden de abrir: {clean}"
        except Exception as exc:
            return f"No se pudo abrir '{clean}': {exc}"


def media_control(action: str) -> str:
    """Controla reproducción multimedia, volumen o bloquea la pantalla de Windows.

    Args:
        action: Acción a ejecutar. Valores válidos:
            'playpause', 'volumedown', 'volumeup', 'mute', 'next', 'prev', 'lock'.

    Returns:
        Confirmación de la acción ejecutada.
    """
    act = action.strip().lower()
    key_map = {
        "playpause": "playpause",
        "volumedown": "volumedown",
        "volumeup": "volumeup",
        "mute": "volumemute",
        "next": "nexttrack",
        "prev": "prevtrack",
    }

    if act == "lock":
        try:
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
            return "Pantalla de Windows bloqueada."
        except Exception as exc:
            return f"Error al bloquear la pantalla: {exc}"

    if act in key_map:
        try:
            with GUI_LOCK:
                pyautogui.press(key_map[act])
            return f"Acción multimedia ejecutada: {act}"
        except Exception as exc:
            return f"Error al simular tecla multimedia '{act}': {exc}"

    valid = ", ".join([*key_map.keys(), "lock"])
    return f"Acción no reconocida '{action}'. Usa una de: {valid}"


# ============================================================================
# 4. AUTONAVEGACIÓN WEB (PLAYWRIGHT) Y CONTROL GUI INTERACTIVO (PYAUTOGUI)
# ============================================================================


def _resolve_browse_url(raw_target: str) -> str:
    """Convierte una URL, dominio o consulta (Google o Wikipedia) en una URL válida."""
    cleaned = raw_target.strip()
    lower = cleaned.lower()
    if lower.startswith(("http://", "https://")):
        return cleaned

    if "wikipedia" in lower:
        query = re.sub(
            r"\b(busca|buscar|investiga|investigar|en|de|sobre|wikipedia)\b",
            " ",
            cleaned,
            flags=re.IGNORECASE,
        )
        query = re.sub(r"\s+", " ", query).strip() or cleaned
        return f"https://es.wikipedia.org/w/index.php?search={quote_plus(query)}"

    if " " not in cleaned and "." in cleaned:
        return f"https://{cleaned}"
    return f"https://www.google.com/search?q={quote_plus(cleaned)}&hl=es"


def _perform_page_action(page: Any, action: str) -> str:
    """Ejecuta una interacción opcional (scroll, click o búsqueda) en Playwright."""
    act = action.strip()
    if not act:
        return ""

    act_lower = act.lower()
    if act_lower.startswith("scroll"):
        pixels = -700 if "up" in act_lower else 700
        if ":" in act:
            try:
                pixels = int(act.split(":", 1)[1].strip())
            except ValueError:
                pass
        page.mouse.wheel(0, pixels)
        return f"Scroll vertical ({pixels}px)"

    if act_lower.startswith("click:"):
        target = act[6:].strip()
        if not target:
            return "Clic omitido (objetivo vacío)"
        try:
            page.get_by_text(target, exact=False).first.click(timeout=5000)
            return f"Clic en texto '{target}'"
        except Exception:
            page.locator(target).first.click(timeout=5000)
            return f"Clic en selector '{target}'"

    # Texto libre: intentar escribir en un campo de búsqueda/texto o hacer clic por texto
    search_selectors = (
        "input[type='search']:visible, "
        "input[name='q']:visible, "
        "textarea[name='q']:visible, "
        "input[type='text']:visible"
    )
    inputs = page.locator(search_selectors)
    if inputs.count() > 0:
        first_input = inputs.first
        first_input.click(timeout=3000)
        first_input.fill(act)
        first_input.press("Enter")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=7000)
        except Exception:
            pass
        return f"Búsqueda/entrada '{act}' enviada"

    page.get_by_text(act, exact=False).first.click(timeout=5000)
    return f"Clic en '{act}'"


def _run_playwright_browse(
    target_url: str,
    action: str,
    wait_sec: int,
) -> tuple[str, str, str, str, Path]:
    """Ejecuta Playwright en un hilo aislado sin conflictos de event loop."""
    from playwright.sync_api import sync_playwright

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TEMP_DIR / f"web_{int(time.time() * 1000)}.png"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1366, "height": 768},
                locale="es-PE",
                timezone_id="America/Lima",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=12000)
            time.sleep(1.0)

            action_note = ""
            if action.strip():
                try:
                    action_note = _perform_page_action(page, action)
                except Exception as act_exc:
                    action_note = f"No se pudo ejecutar la acción '{action}': {act_exc}"

            time.sleep(wait_sec)
            page.screenshot(path=str(out_path), full_page=False)
            title = (page.title() or "").strip()
            final_url = page.url
            try:
                body_text = page.inner_text("body") or ""
            except Exception:
                body_text = ""
            return title, final_url, body_text, action_note, out_path
        finally:
            browser.close()


def browse_and_capture(
    url: str,
    search_or_action: str = "",
    wait_seconds: int = 4,
    send_to_whatsapp: bool = False,
) -> str:
    """Abre una URL o busca en Google con Chromium, interactúa opcionalmente, toma captura y extrae el texto.

    Args:
        url: URL completa (https://...), dominio (ej. 'unsa.edu.pe') o términos de búsqueda en Google.
        search_or_action: Acción opcional tras cargar la página:
            'scroll' / 'scroll up' / 'scroll:1200' para desplazarse,
            'click:<texto o selector>' para hacer clic,
            o texto libre para rellenar un buscador y presionar Enter.
        wait_seconds: Segundos a esperar tras la acción antes de capturar (1 a 20, por defecto 4).
        send_to_whatsapp: Si True, encola la captura web para enviarla al usuario; por defecto False (solo visión interna).

    Returns:
        Título, URL final, ruta de la captura y texto visible extraído.
    """
    if not url.strip():
        return "Debes indicar una URL o término de búsqueda."

    target_url = _resolve_browse_url(url)
    wait_sec = max(1, min(int(wait_seconds), 20))

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                _run_playwright_browse,
                target_url,
                search_or_action,
                wait_sec,
            )
            title, final_url, raw_text, action_note, out_path = future.result(timeout=65)
    except Exception as exc:
        return f"Error al navegar con Playwright hacia '{target_url}': {exc}"

    record_internal_screenshot(out_path)
    if send_to_whatsapp:
        _enqueue_image(out_path)

    cleaned_lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    collapsed_text = re.sub(r"\s{2,}", " ", "\n".join(cleaned_lines))
    if len(collapsed_text) > 3500:
        collapsed_text = collapsed_text[:3500].rstrip() + "\n...[texto truncado]..."

    cap_note = "encolada para WhatsApp" if send_to_whatsapp else "captura interna de visión"
    parts = [
        f"Navegación completada ({cap_note}: {out_path}):",
        f"- Título: {title or '(sin título)'}",
        f"- URL final: {final_url}",
    ]
    if action_note:
        parts.append(f"- Acción: {action_note}")
    parts.append(
        f"\nTexto visible extraído:\n{wrap_external_data(collapsed_text or '(Página sin texto visible)')}"
    )
    return "\n".join(parts)


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Extrae el primer objeto JSON válido desde una respuesta de texto del modelo."""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _vision_locate_on_screen(
    monitor_idx: int = 0,
    goal_prompt: str = "",
    target_window_title: str = "Brave",
) -> dict[str, Any]:
    """Captura un monitor completo y pide a Gemini Vision (v1beta) coordenadas normalizadas 0..1000."""
    req_mon = int(monitor_idx) if monitor_idx and int(monitor_idx) > 0 else 0
    cap = capture_monitor_image(
        monitor_number=req_mon,
        target_window_title=target_window_title,
        enqueue=False,
    )
    if not cap.get("ok"):
        return {
            "ok": False,
            "found": False,
            "status": "error",
            "error": str(cap.get("error", "No se pudo capturar pantalla para visión.")),
        }

    mon_idx_used = int(cap.get("monitor_index") or req_mon or 1)
    monitors = _get_monitors_dynamic()
    monitor_info: dict[str, int] = dict(cap.get("monitor") or {})
    for m in monitors:
        if int(m.get("id", 0)) == mon_idx_used:
            monitor_info = dict(m["mss_rect"])
            break
    if not monitor_info or int(monitor_info.get("width", 0)) <= 0:
        monitor_info = {
            "left": 0,
            "top": 0,
            "width": int(cap.get("width") or 1920),
            "height": int(cap.get("height") or 1080),
        }

    priv_ok, priv_reason = assert_screen_privacy(monitor_info)
    if not priv_ok:
        return {
            "ok": False,
            "found": False,
            "status": "privacy_blocked",
            "error": priv_reason,
        }

    api_key = get_env("GEMINI_API_KEY")
    if not api_key or api_key == "TU_GEMINI_API_KEY_AQUI":
        return {
            "ok": False,
            "found": False,
            "status": "error",
            "error": "GEMINI_API_KEY no configurada para visión.",
            "monitor": monitor_info,
            "monitor_index": mon_idx_used,
            "screenshot_path": cap["path"],
        }

    candidates = filter_available_models([*GEMINI_MODELS_FAST, GEMINI_MODEL])
    if not candidates:
        return {
            "ok": False,
            "found": False,
            "status": "error",
            "error": "No hay modelos rápidos disponibles para visión en este momento.",
            "monitor": monitor_info,
            "monitor_index": mon_idx_used,
            "screenshot_path": cap["path"],
        }

    prompt_text = (
        "Eres un agente de control visual de PC. Mira esta captura de pantalla completa del monitor. "
        f"Tu objetivo es: {goal_prompt}. "
        "Calcula la posición exacta del elemento usando coordenadas normalizadas de 0 a 1000 "
        "(donde x=0 es borde izquierdo, x=1000 borde derecho, y=0 borde superior, y=1000 borde inferior). "
        'Responde ÚNICAMENTE con JSON válido: {"found": true/false, "x": int, "y": int, '
        '"status": "ready"|"generating"|"popup"|"finished_download"|"finished_code"|"not_found"|"error", "notes": "..."}.'
    )

    try:
        img_bytes = Path(str(cap["path"])).read_bytes()
    except OSError as exc:
        return {
            "ok": False,
            "found": False,
            "status": "error",
            "error": f"No se pudo leer la captura para visión: {exc}",
            "monitor": monitor_info,
            "monitor_index": mon_idx_used,
        }

    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
    parts = [
        types.Part.from_bytes(data=img_bytes, mime_type="image/png"),
        types.Part.from_text(text=prompt_text),
    ]

    last_err = ""
    for model_name in candidates:
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=parts,
                config=types.GenerateContentConfig(temperature=0.1),
            )
            clear_model_cooldown(model_name)
            raw_reply = (resp.text or "").strip()
            data = _extract_json_object(raw_reply)
            if not data:
                last_err = f"{model_name}: JSON inválido ({raw_reply[:120]})"
                continue

            coords = data.get("coordinates")
            raw_x = coords[0] if isinstance(coords, (list, tuple)) and len(coords) >= 2 else data.get("x", 0)
            raw_y = coords[1] if isinstance(coords, (list, tuple)) and len(coords) >= 2 else data.get("y", 0)
            try:
                x_norm = max(0, min(1000, int(round(float(raw_x)))))
                y_norm = max(0, min(1000, int(round(float(raw_y)))))
            except (TypeError, ValueError):
                x_norm, y_norm = 0, 0

            real_x = int(monitor_info["left"]) + int((x_norm / 1000.0) * int(monitor_info["width"]))
            real_y = int(monitor_info["top"]) + int((y_norm / 1000.0) * int(monitor_info["height"]))
            found = bool(data.get("found", False)) and (x_norm > 0 or y_norm > 0)
            status = str(data.get("status") or ("ready" if found else "not_found")).strip().lower()
            notes = str(data.get("notes") or "").strip()
            return {
                "ok": True,
                "found": found,
                "x": x_norm,
                "y": y_norm,
                "real_x": real_x,
                "real_y": real_y,
                "status": status,
                "notes": notes,
                "model": model_name,
                "monitor": monitor_info,
                "monitor_index": mon_idx_used,
                "screenshot_path": cap["path"],
            }
        except Exception as exc:
            err_str = str(exc)
            classify_and_record_model_error(model_name, err_str)
            last_err = f"{model_name}: {err_str}"
            continue

    return {
        "ok": False,
        "found": False,
        "status": "error",
        "error": last_err or "Todos los modelos de visión fallaron.",
        "monitor": monitor_info,
        "monitor_index": mon_idx_used,
        "screenshot_path": cap["path"],
    }


def gui_click_and_type(
    x: int | None = None,
    y: int | None = None,
    text_to_type: str = "",
    press_enter: bool = False,
    hotkey: str = "",
    visual_target: str = "",
    send_to_whatsapp: bool = False,
) -> str:
    """Controla mouse y teclado (por coordenadas o visión AI en pantalla) y toma captura de resultado.

    Args:
        x: Coordenada horizontal X en píxeles donde hacer clic (opcional).
        y: Coordenada vertical Y en píxeles donde hacer clic (opcional).
        text_to_type: Texto a escribir en la ventana activa (opcional).
        press_enter: Si es True, presiona la tecla Enter al final.
        hotkey: Atajo de teclado separado por '+' (ej. 'alt+tab', 'ctrl+t', 'win+d').
        visual_target: Descripción visual del botón/campo donde la IA debe ver la pantalla y hacer clic
            (se usa automáticamente cuando no se indican coordenadas x, y).
        send_to_whatsapp: Si True, encola la captura posterior para WhatsApp; por defecto False
            (captura interna para que el agente verifique el resultado sin spamear).

    Returns:
        Resumen de acciones GUI ejecutadas y estado de la captura de pantalla posterior.
    """
    actions_done: list[str] = []
    if (x is None or y is None) and visual_target and visual_target.strip():
        _check_not_cancelled()
        vis = _vision_locate_on_screen(0, visual_target.strip(), target_window_title="")
        if not vis.get("ok") or not vis.get("found"):
            detail = vis.get("notes") or vis.get("error") or "elemento no visible"
            return f"Visión no pudo localizar '{visual_target.strip()}' en pantalla: {detail}"
        x = int(vis["real_x"])
        y = int(vis["real_y"])
        actions_done.append(
            f"Visión localizó '{visual_target.strip()}' -> ({x}, {y}) [norm {vis['x']},{vis['y']}]"
        )

    try:
        with GUI_LOCK:
            _check_not_cancelled()
            pyautogui.FAILSAFE = False
            pyautogui.PAUSE = 0.15

            if x is not None and y is not None:
                _check_not_cancelled()
                px, py = int(x), int(y)
                pyautogui.click(x=px, y=py)
                time.sleep(0.3)
                actions_done.append(f"Clic en ({px}, {py})")

            if hotkey.strip():
                _check_not_cancelled()
                keys = [k.strip().lower() for k in hotkey.split("+") if k.strip()]
                if keys:
                    pyautogui.hotkey(*keys)
                    time.sleep(0.4)
                    actions_done.append(f"Atajo ejecutado: {'+'.join(keys)}")

            if text_to_type:
                _check_not_cancelled()
                if text_to_type.isascii():
                    pyautogui.write(text_to_type, interval=0.02)
                else:
                    _copy_to_clipboard(text_to_type)
                    pyautogui.hotkey("ctrl", "v")
                time.sleep(0.2)
                preview = text_to_type if len(text_to_type) <= 60 else text_to_type[:57] + "..."
                actions_done.append(f"Texto escrito: '{preview}'")

            if press_enter:
                _check_not_cancelled()
                pyautogui.press("enter")
                actions_done.append("Tecla Enter presionada")
    except TaskCancelledError as cancel_exc:
        return f"Acción GUI cancelada limpiamente: {cancel_exc}"
    except Exception as exc:
        return f"Error al ejecutar control GUI con pyautogui: {exc}"

    if not actions_done:
        return (
            "No se especificó ninguna acción GUI. Indica coordenadas (x, y), `visual_target`, "
            "`text_to_type`, `hotkey` o `press_enter=True`."
        )

    _check_not_cancelled()
    time.sleep(0.5)
    screenshot_status = take_screenshot(send_to_whatsapp=send_to_whatsapp)
    return (
        "Acciones GUI ejecutadas:\n"
        + "\n".join(f"- {item}" for item in actions_done)
        + f"\n{screenshot_status}"
    )


# ============================================================================
# 5. BRAVE (GOOGLE DOCS / GEMINI WEB) Y CONTROL DE VENTANAS WINDOWS
# ============================================================================

DOCS_BACKUP_DIR = Path.home() / "Desktop" / "Mis_Documentos_Agente"


def _find_brave_executable() -> str | None:
    """Busca la ruta del ejecutable de Brave Browser en Windows."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.join(local_app_data, r"BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return shutil.which("brave") or shutil.which("brave.exe")


_RECENT_BRAVE_LOCK = threading.Lock()
_RECENT_BRAVE_URLS: dict[str, float] = {}
_BRAVE_URL_DEDUP_SECONDS = 15.0


def _open_url_in_brave(url: str, new_window: bool = False) -> str:
    """Abre una URL en Brave Browser con candado anti-duplicados de 15 segundos."""
    clean_url = (url or "").strip()
    now_ts = time.monotonic()
    with _RECENT_BRAVE_LOCK:
        expired = [u for u, ts in _RECENT_BRAVE_URLS.items() if now_ts - ts >= _BRAVE_URL_DEDUP_SECONDS]
        for u in expired:
            _RECENT_BRAVE_URLS.pop(u, None)
        last_ts = _RECENT_BRAVE_URLS.get(clean_url)
        if last_ts is not None and (now_ts - last_ts) < _BRAVE_URL_DEDUP_SECONDS:
            return "already_opened_recently"
        _RECENT_BRAVE_URLS[clean_url] = now_ts

    brave_path = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    if not os.path.exists(brave_path):
        brave_path = _find_brave_executable() or ""

    if brave_path and os.path.exists(brave_path):
        cmd = [brave_path]
        if new_window:
            cmd.append("--new-window")
        cmd.append(clean_url)
        subprocess.Popen(cmd, shell=False)
        return brave_path

    os.startfile(clean_url)
    return "os.startfile"


def _copy_to_clipboard(text: str) -> None:
    """Copia texto UTF-8 al portapapeles de Windows."""
    if pyperclip is not None:
        try:
            pyperclip.copy(text)
            return
        except Exception:
            pass
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", "$input | Set-Clipboard"],
        input=text,
        text=True,
        encoding="utf-8",
        timeout=5,
        check=False,
    )


def _force_focus_window(hwnd: int, maximize: bool = False) -> bool:
    """Bypass del bloqueo SetForegroundWindow en Windows 11 mediante ALT + AttachThreadInput."""
    if not hwnd:
        return False
    if win32gui is not None and win32con is not None and win32process is not None and win32api is not None:
        attached_current = False
        attached_fg = False
        fg_thread = 0
        target_thread = 0
        current_thread = 0
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)

            # Pulso tecla ALT + AttachThreadInput para evitar "Acceso denegado" en Windows 11
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)

            fg_hwnd = win32gui.GetForegroundWindow()
            fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd) if fg_hwnd else (0, 0)
            target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
            current_thread = win32api.GetCurrentThreadId()

            if target_thread and current_thread != target_thread:
                attached_current = bool(
                    ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, True)
                )
            if fg_thread and target_thread and fg_thread != target_thread:
                attached_fg = bool(
                    ctypes.windll.user32.AttachThreadInput(fg_thread, target_thread, True)
                )

            win32gui.SetForegroundWindow(hwnd)
            win32gui.BringWindowToTop(hwnd)
            if maximize:
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
            return True
        except Exception as exc:
            print(f"[WARN] _force_focus_window fallback: {exc}", flush=True)
            return False
        finally:
            if attached_current:
                ctypes.windll.user32.AttachThreadInput(current_thread, target_thread, False)
            if attached_fg:
                ctypes.windll.user32.AttachThreadInput(fg_thread, target_thread, False)

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    sw_mode = 3 if maximize else 9
    fg_hwnd = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
    cur_tid = kernel32.GetCurrentThreadId()
    attached = False
    if fg_tid and fg_tid != cur_tid:
        attached = bool(user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.ShowWindow(hwnd, sw_mode)
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def _focus_window_by_keywords(
    keywords: list[str],
    maximize: bool = True,
) -> tuple[bool, str]:
    """Busca una ventana visible en Windows por título y la trae al primer plano."""
    hwnd, found_title = _find_window_handle(keywords)

    if hwnd and _force_focus_window(hwnd, maximize=maximize):
        return True, found_title

    # Respaldo compacto en una sola invocación PowerShell con WScript.Shell.AppActivate
    valid_kws = [kw for kw in keywords if kw]
    if valid_kws:
        ps_items = ", ".join(f"'{kw.replace(chr(39), chr(39) * 2)}'" for kw in valid_kws)
        ps = (
            "$ws = New-Object -ComObject WScript.Shell; "
            f"foreach ($k in @({ps_items})) {{ "
            "if ($ws.AppActivate($k)) { Write-Output $k; exit 0 } }; "
            "exit 1"
        )
        try:
            res = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if res.returncode == 0:
                matched = (res.stdout or "").strip() or valid_kws[0]
                return True, matched
        except Exception:
            pass

    return False, ""


def _launch_and_focus_brave(
    url: str,
    wait_seconds: float = 2.5,
    new_window: bool = False,
    maximize: bool = True,
    focus_hints: list[str] | None = None,
) -> bool:
    """Abre físicamente Brave real con la URL indicada y fuerza su enfoque en primer plano."""
    _open_url_in_brave(url, new_window=new_window)
    time.sleep(max(0.5, float(wait_seconds)))
    hints = list(focus_hints) if focus_hints else ["- Brave", "Brave"]
    if "Brave" not in hints:
        hints.append("Brave")
    focused, _ = _focus_window_by_keywords(hints, maximize=maximize)
    return bool(focused)


def _ensure_google_doc_content(title: str, content: str) -> str:
    """Garantiza que Google Docs reciba contenido estructurado incluso si el borrador inicial es corto."""
    clean_title = (title or "Documento").strip()
    clean_content = (content or "").strip()
    if len(clean_content) >= 50:
        return clean_content

    api_key = get_env("GEMINI_API_KEY")
    if api_key and api_key != "TU_GEMINI_API_KEY_AQUI":
        prompt = (
            f"Redacta apuntes académicos completos y bien estructurados en español sobre el tema:\n"
            f"Título: {clean_title}\n"
            + (f"Notas base: {clean_content}\n" if clean_content else "")
            + "\nIncluye introducción, secciones claras con subtítulos en texto limpio, conceptos clave, "
            "ejemplos prácticos y conclusiones breves (sin sintaxis HTML)."
        )
        candidates = filter_available_models(GEMINI_MODELS_FAST)
        try:
            client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
            for candidate_model in candidates:
                try:
                    resp = client.models.generate_content(
                        model=candidate_model,
                        contents=prompt,
                        config=types.GenerateContentConfig(temperature=0.4),
                    )
                    generated = (getattr(resp, "text", None) or "").strip()
                    if len(generated) >= 50:
                        clear_model_cooldown(candidate_model)
                        return generated
                except Exception as exc:
                    classify_and_record_model_error(candidate_model, str(exc))
        except Exception:
            pass

    base_note = f"{clean_content}\n\n" if clean_content else ""
    return (
        f"{base_note}"
        f"1. Introducción\n"
        f"Documento de estudio y síntesis sobre {clean_title}.\n\n"
        f"2. Objetivos y Conceptos Clave\n"
        f"- Comprender los fundamentos principales de {clean_title}.\n"
        f"- Identificar definiciones, arquitectura y casos de aplicación.\n\n"
        f"3. Desarrollo del Tema\n"
        f"Espacio organizado para desarrollar definiciones, ejemplos resueltos y análisis crítico.\n\n"
        f"4. Conclusiones\n"
        f"Resumen de puntos clave y siguientes pasos de estudio."
    )


def create_google_doc_in_brave(title: str, content: str = "") -> str:
    """Genera un documento estructurado (.docx compatible con Google Docs y Word) vía Gemini Web en Brave.

    Args:
        title: Título o tema principal del documento.
        content: Detalles, esquema o notas opcionales para enriquecer el documento.

    Returns:
        Resumen del documento `.docx` compilado en generated_projects/, respaldo Markdown y captura.
    """
    clean_title = (title or "Documento académico").strip()
    clean_content = (content or "").strip()

    DOCS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r'[<>:"/\\|?*]+', "_", clean_title).strip(" .") or "documento"
    backup_path = DOCS_BACKUP_DIR / f"{safe_stem[:80]}.md"
    if clean_content:
        backup_path.write_text(f"# {clean_title}\n\n{clean_content}\n", encoding="utf-8")

    goal_text = (
        f"Documento detallado y esquema profesional sobre '{clean_title}'. Contenido adicional e instrucciones: {clean_content}"
        if clean_content
        else f"Documento completo, esquema estructurado y apuntes detallados sobre '{clean_title}'"
    )
    factory_res = build_with_heavy_web_ai(
        user_goal=goal_text,
        target_ai="gemini",
        output_format="docx",
        pages_or_slides=6,
        wait_seconds=600,
    )
    return (
        f"{factory_res}\n"
        f"- Formato .docx compatible con Google Docs y Word."
        + (f"\n- Respaldo Markdown local: {backup_path}" if backup_path.exists() else "")
    )


def ask_web_ai_in_brave(
    prompt_for_web_ai: str,
    wait_seconds: int = 15,
    send_to_whatsapp: bool = False,
) -> str:
    """Abre Gemini Web en Brave (sesión Pro del usuario), envía un prompt y captura la respuesta.

    Args:
        prompt_for_web_ai: Consulta o instrucción a enviar en la web de Gemini.
        wait_seconds: Segundos de espera tras presionar Enter (3 a 60, por defecto 15).
        send_to_whatsapp: Si True, envía la captura resultante al usuario; por defecto False (solo visión interna).

    Returns:
        Confirmación del envío en Brave y resultado de la captura de pantalla.
    """
    prompt_clean = (prompt_for_web_ai or "").strip()
    if not prompt_clean:
        return "El prompt para Gemini Web no puede estar vacío."

    wait_sec = max(3, min(int(wait_seconds), 60))
    try:
        with GUI_LOCK:
            focused = _launch_and_focus_brave(
                "https://gemini.google.com/app",
                wait_seconds=5.5,
                focus_hints=["Gemini", "- Brave", "Brave"],
            )
            _copy_to_clipboard(prompt_clean)
            time.sleep(0.4)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.5)
            pyautogui.press("enter")
    except Exception as exc:
        return f"No se pudo abrir Gemini Web en Brave: {exc}"

    time.sleep(wait_sec)
    screenshot_status = take_screenshot(
        target_window_title="Brave",
        send_to_whatsapp=send_to_whatsapp,
    )
    focus_note = "Brave / Gemini Web" if focused else "Brave (ventana no confirmada por título)"
    return (
        f"Prompt enviado a Gemini Web en Brave (Ventana: {focus_note}) "
        f"tras esperar {wait_sec}s.\n{screenshot_status}"
    )


def control_app_window(
    app_name: str,
    action: str = "focus",
    text_or_shortcut: str = "",
    target_path_or_url: str = "",
    send_to_whatsapp: bool = False,
) -> str:
    """Controla ventanas de Antigravity, Visual Studio Code o Brave en Windows.

    Args:
        app_name: Aplicación objetivo: 'Antigravity', 'Visual Studio Code' ('Code') o 'Brave'.
        action: Acción a realizar: 'focus', 'open' o 'type_or_shortcut'.
        text_or_shortcut: Atajo (ej. 'ctrl+l', 'ctrl+n', 'ctrl+shift+p') o texto para pegar.
        target_path_or_url: Ruta/carpeta para abrir en Antigravity/VS Code, o URL para Brave.
        send_to_whatsapp: Si True, encola la captura resultante para WhatsApp; por defecto False.

    Returns:
        Estado de la operación y resultado de la captura de pantalla.
    """
    app_lower = (app_name or "").strip().lower()
    act_lower = (action or "focus").strip().lower()

    if "antigravity" in app_lower:
        canonical = "Antigravity"
        keywords = ["Antigravity"]
    elif "code" in app_lower or "vscode" in app_lower or "vs code" in app_lower:
        canonical = "Visual Studio Code"
        keywords = ["Visual Studio Code", "Code"]
    elif "brave" in app_lower:
        canonical = "Brave"
        keywords = ["Brave"]
    else:
        return (
            f"Aplicación '{app_name}' no soportada. "
            "Usa 'Antigravity', 'Visual Studio Code' o 'Brave'."
        )

    if act_lower == "open":
        target = (target_path_or_url or "").strip()
        try:
            if canonical == "Brave":
                _open_url_in_brave(target or "https://www.google.com")
            elif canonical == "Visual Studio Code":
                code_cmd = shutil.which("code.cmd") or shutil.which("code") or "code"
                args = [code_cmd, target] if target else [code_cmd]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                local_app = os.environ.get("LOCALAPPDATA", "")
                agy_exe = os.path.join(local_app, r"Programs\Antigravity\Antigravity.exe")
                exe = agy_exe if os.path.exists(agy_exe) else (shutil.which("agy") or "Antigravity")
                args = [exe, target] if target else [exe]
                subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            return f"Error al abrir '{canonical}': {exc}"

        time.sleep(3.0)
        with GUI_LOCK:
            focused, win_title = _focus_window_by_keywords(keywords, maximize=True)
        screenshot_status = take_screenshot(
            target_window_title=win_title or canonical,
            send_to_whatsapp=send_to_whatsapp,
        )
        return (
            f"Orden de apertura enviada a {canonical}"
            + (f" con '{target}'" if target else "")
            + f". Ventana enfocada: {win_title if focused else 'no confirmada'}.\n"
            + screenshot_status
        )

    with GUI_LOCK:
        _check_not_cancelled()
        focused, win_title = _focus_window_by_keywords(keywords, maximize=True)
    if not focused:
        return f"No se encontró ninguna ventana visible para '{canonical}'."

    if act_lower == "close":
        hwnd, found_title = _find_window_handle(keywords)
        if not hwnd:
            return f"No se encontró el manejador de ventana para cerrar '{canonical}'."
        try:
            WM_CLOSE = 0x0010
            ctypes.windll.user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0)
            return f"Cierre suave (WM_CLOSE) enviado a {canonical} ({found_title or win_title})."
        except Exception as exc:
            return f"Error enviando WM_CLOSE a '{canonical}': {exc}"

    if act_lower in ("move_monitor_1", "move_monitor_2", "move_monitor_3", "fullscreen"):
        hwnd, found_title = _find_window_handle(keywords)
        if not hwnd:
            return f"No se encontró el manejador de ventana para '{canonical}'."
        monitors = _get_monitors_dynamic()
        if not monitors:
            return "No se detectaron monitores activos."
        if act_lower == "fullscreen":
            target_mon = monitors[0]
            with GUI_LOCK:
                _check_not_cancelled()
                moved = _move_window_to_monitor(hwnd, target_mon, press_f11=True)
            screenshot_status = take_screenshot(
                target_window_title=found_title or canonical,
                send_to_whatsapp=send_to_whatsapp,
            )
            return (
                f"{canonical} puesto en pantalla completa ({'OK' if moved else 'parcial'}).\n"
                + screenshot_status
            )

        mon_num = int(act_lower.split("_")[-1])
        target_mon = next((m for m in monitors if int(m.get("id", 0)) == mon_num), None)
        if target_mon is None:
            return f"No existe el monitor {mon_num} (monitores detectados: {len(monitors)})."
        with GUI_LOCK:
            _check_not_cancelled()
            moved = _move_window_to_monitor(hwnd, target_mon, press_f11=False)
        time.sleep(0.5)
        screenshot_status = take_screenshot(
            monitor_number=mon_num,
            target_window_title=found_title or canonical,
            send_to_whatsapp=send_to_whatsapp,
        )
        return (
            f"{canonical} ({found_title or win_title}) movido al monitor {mon_num} ({'OK' if moved else 'error'}).\n"
            + screenshot_status
        )

    if act_lower == "focus":
        _check_not_cancelled()
        time.sleep(0.5)
        screenshot_status = take_screenshot(
            target_window_title=win_title or canonical,
            send_to_whatsapp=send_to_whatsapp,
        )
        return f"Ventana enfocada y maximizada: {win_title}.\n{screenshot_status}"

    if act_lower == "type_or_shortcut":
        payload = (text_or_shortcut or "").strip()
        if not payload:
            return "Debes indicar `text_or_shortcut` cuando `action='type_or_shortcut'`."

        time.sleep(0.8)
        special_keys = {"enter", "esc", "escape", "tab", "f5", "f11", "space", "backspace"}

        with GUI_LOCK:
            _check_not_cancelled()
            focused, win_title = _focus_window_by_keywords(keywords, maximize=True)
            time.sleep(0.25)
            pyautogui.FAILSAFE = False
            if "+" in payload:
                keys = [k.strip().lower() for k in payload.split("+") if k.strip()]
                pyautogui.hotkey(*keys)
                op_desc = f"Atajo ejecutado: {'+'.join(keys)}"
            elif payload.lower() in special_keys:
                pyautogui.press(payload.lower())
                op_desc = f"Tecla presionada: {payload.lower()}"
            else:
                _copy_to_clipboard(payload)
                pyautogui.hotkey("ctrl", "v")
                time.sleep(0.2)
                pyautogui.press("enter")
                preview = payload if len(payload) <= 60 else payload[:57] + "..."
                op_desc = f"Texto pegado + Enter: '{preview}'"

        _check_not_cancelled()
        time.sleep(2.0)
        screenshot_status = take_screenshot(
            target_window_title=win_title or canonical,
            send_to_whatsapp=send_to_whatsapp,
        )
        return f"{canonical} ({win_title}) -> {op_desc}.\n{screenshot_status}"

    return (
        f"Acción '{action}' no válida. Usa 'focus', 'open', 'type_or_shortcut', "
        "'move_monitor_1', 'move_monitor_2', 'move_monitor_3', 'fullscreen' o 'close'."
    )


# ============================================================================
# 6. ARCHIVOS LOCALES (ENVÍO POR WHATSAPP, LECTURA PDF) Y CLAUDE CODE CLI
# ============================================================================

_SKIP_SCAN_EXTENSIONS = {".tmp", ".crdownload", ".part", ".ini", ".lnk"}
_SKIP_SCAN_DIRS = {".git", ".venv", "node_modules", "__pycache__", "auth_info_baileys"}
_TEXT_READABLE_EXTENSIONS = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".json",
    ".csv",
    ".log",
    ".html",
    ".css",
    ".xml",
    ".yaml",
    ".yml",
    ".ps1",
    ".bat",
    ".sql",
}


def _collect_candidate_files(max_depth: int = 2) -> list[Path]:
    """Recolecta archivos de Descargas, Escritorio, Documentos y respaldo del agente."""
    home = Path.home()
    roots = [
        home / "Downloads",
        home / "Descargas",
        home / "Desktop",
        home / "Escritorio",
        home / "Documents",
        home / "Documentos",
        DOCS_BACKUP_DIR,
    ]
    seen_roots: list[Path] = []
    for r in roots:
        if r.exists() and r.is_dir() and r not in seen_roots:
            seen_roots.append(r)

    files: list[Path] = []
    for root in seen_roots:
        try:
            for item in root.iterdir():
                if item.is_file() and item.suffix.lower() not in _SKIP_SCAN_EXTENSIONS:
                    files.append(item)
                elif item.is_dir() and max_depth >= 2 and item.name not in _SKIP_SCAN_DIRS:
                    try:
                        for sub in item.iterdir():
                            if sub.is_file() and sub.suffix.lower() not in _SKIP_SCAN_EXTENSIONS:
                                files.append(sub)
                    except OSError:
                        continue
        except OSError:
            continue

    # Incluir archivos de nivel superior en BASE_DIR (excluyendo carpetas internas)
    if BASE_DIR.exists():
        try:
            for item in BASE_DIR.iterdir():
                if item.is_file() and item.suffix.lower() not in _SKIP_SCAN_EXTENSIONS:
                    files.append(item)
        except OSError:
            pass

    return files


def _find_local_file(file_query: str) -> tuple[Path | None, str]:
    """Busca un archivo por ruta directa, consulta temporal ('último pdf') o coincidencia parcial."""
    raw = (file_query or "").strip().strip('"').strip("'")
    if not raw:
        return None, "Debes indicar una ruta o nombre/descripción del archivo."

    direct = Path(raw).expanduser()
    if direct.exists() and direct.is_file():
        return direct.resolve(), ""

    # Comprobar si es relativo al proyecto o carpetas de usuario
    home = Path.home()
    for base in (BASE_DIR, home / "Downloads", home / "Desktop", home / "Documents", DOCS_BACKUP_DIR):
        candidate = (base / raw).expanduser()
        if candidate.exists() and candidate.is_file():
            return candidate.resolve(), ""

    candidates = _collect_candidate_files(max_depth=2)
    if not candidates:
        return None, "No se encontraron archivos accesibles en Descargas, Escritorio o Documentos."

    q_lower = raw.lower()
    is_latest_pdf = any(
        phrase in q_lower
        for phrase in (
            "último pdf",
            "ultimo pdf",
            "pdf reciente",
            "pdf más reciente",
            "pdf mas reciente",
            "latest pdf",
        )
    )
    if is_latest_pdf:
        pdfs = [p for p in candidates if p.suffix.lower() == ".pdf"]
        if not pdfs:
            return None, "No se encontró ningún archivo PDF en Descargas, Escritorio o Documentos."
        pdfs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return pdfs[0].resolve(), ""

    is_latest_any = any(
        phrase in q_lower
        for phrase in (
            "último archivo",
            "ultimo archivo",
            "archivo reciente",
            "archivo más reciente",
            "archivo mas reciente",
            "latest file",
        )
    )
    if is_latest_any:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0].resolve(), ""

    # Búsqueda por nombre parcial o tokens
    tokens = [t for t in re.split(r"[\s,;]+", q_lower) if len(t) >= 2]
    scored: list[tuple[int, float, Path]] = []
    for path in candidates:
        name_lower = path.name.lower()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        if q_lower == name_lower:
            scored.append((3, mtime, path))
        elif q_lower in name_lower:
            scored.append((2, mtime, path))
        elif tokens and all(tok in name_lower for tok in tokens):
            scored.append((1, mtime, path))

    if not scored:
        return (
            None,
            f"No encontré ningún archivo que coincida con '{raw}' en Descargas, Escritorio ni Documentos.",
        )

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2].resolve(), ""


def _format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / (1024 * 1024):.2f} MB"


def send_file_to_whatsapp(
    file_path_or_query: str,
    allow_existing_file: bool = False,
) -> str:
    """Busca un archivo local (PDF, código, Word, ZIP, etc.) y lo envía como documento adjunto por WhatsApp.

    Args:
        file_path_or_query: Ruta completa o búsqueda en Descargas/Escritorio/Documentos
            (ej. 'último pdf descargado', 'informe', 'C:\\ruta\\archivo.pdf').
        allow_existing_file: Usar True ÚNICAMENTE si el usuario pidió explícitamente enviar un
            archivo que ya tenía guardado en su PC antes de la tarea actual.

    Returns:
        Confirmación de que el archivo fue encolado para su envío por WhatsApp o detalle del error.
    """
    found_path, err = _find_local_file(file_path_or_query)
    if err or found_path is None:
        return err or "Archivo no encontrado."

    if is_secret_path(str(found_path)) and should_require_confirmation():
        return request_confirmation_for_current_task(
            action_name="send_file_to_whatsapp",
            args_dict={
                "file_path_or_query": str(found_path),
                "allow_existing_file": bool(allow_existing_file),
            },
            reason="Envío de archivo de secretos por WhatsApp",
            file_to_hash=found_path,
            task_id=get_current_task_id(),
        )

    vinfo = verify_file_info(found_path)
    if not vinfo.get("ok"):
        return f"ERROR: El archivo no pasó la verificación previa al envío: {vinfo.get('error')}"

    task_started_at = get_task_started_at()
    try:
        file_mtime = os.path.getmtime(found_path)
    except OSError as exc:
        return f"No se pudo leer la fecha de modificación de '{found_path}': {exc}"

    if task_started_at is not None and file_mtime < task_started_at and not allow_existing_file:
        mtime_str = datetime.fromtimestamp(file_mtime, tz=LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        start_str = datetime.fromtimestamp(task_started_at, tz=LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
        return (
            f"ERROR [CANDADO_ARCHIVO_ANTIGUO]: Se bloqueó el envío de '{found_path.name}' porque es "
            f"anterior al inicio de la tarea actual (modificado: {mtime_str} < inicio_tarea: {start_str}). "
            "No sustituyas una descarga fallida con un archivo viejo. Si el usuario pidió explícitamente "
            "enviar ese archivo que ya tenía en su PC, vuelve a llamar con `allow_existing_file=True`."
        )

    size_bytes = int(vinfo["size_bytes"])
    max_bytes = 64 * 1024 * 1024
    if size_bytes > max_bytes:
        return (
            f"El archivo '{found_path.name}' pesa {_format_size(size_bytes)} y supera el "
            "límite de 64 MB permitido para envío directo por WhatsApp."
        )

    _enqueue_file(found_path)
    return (
        f"Archivo encolado para enviar por WhatsApp como documento adjunto:\n"
        f"- Nombre: {found_path.name}\n"
        f"- Tamaño: {_format_size(size_bytes)}\n"
        f"- Firma: {vinfo['signature_note']}\n"
        f"- Ruta: {found_path}"
    )


def read_local_pdf_or_file(file_query: str) -> str:
    """Busca un archivo PDF o de texto en Descargas, Escritorio o Documentos y extrae su contenido.

    Args:
        file_query: Ruta o nombre aproximado del archivo (ej. 'último pdf descargado', 'silabo.pdf', 'notas.txt').

    Returns:
        Texto extraído del archivo para que el agente lo resuma o analice.
    """
    found_path, err = _find_local_file(file_query)
    if err or found_path is None:
        return err or "Archivo no encontrado."

    if is_secret_path(str(found_path)) and should_require_confirmation():
        return request_confirmation_for_current_task(
            action_name="read_local_pdf_or_file",
            args_dict={"file_query": str(found_path)},
            reason="Lectura de archivo de secretos",
            file_to_hash=found_path,
            task_id=get_current_task_id(),
        )

    ext = found_path.suffix.lower()
    max_chars = 9000

    if ext == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(found_path))
            total_pages = len(reader.pages)
            chunks: list[str] = []
            current_len = 0
            for idx, page in enumerate(reader.pages, start=1):
                page_text = (page.extract_text() or "").strip()
                if not page_text:
                    continue
                block = f"--- Página {idx}/{total_pages} ---\n{page_text}"
                if current_len + len(block) > max_chars:
                    remaining = max(0, max_chars - current_len)
                    if remaining > 100:
                        chunks.append(block[:remaining].rstrip() + "\n...[contenido truncado]...")
                    break
                chunks.append(block)
                current_len += len(block)

            body = "\n\n".join(chunks).strip()
            if not body:
                return (
                    f"El PDF '{found_path.name}' ({total_pages} páginas) no contiene texto "
                    "extraíble (podría ser un documento escaneado como imagen)."
                )
            return (
                f"Archivo PDF leído: {found_path.name} ({total_pages} páginas | "
                f"{_format_size(found_path.stat().st_size)})\nRuta: {found_path}\n\n{body}"
            )
        except Exception as exc:
            return f"Error al extraer texto del PDF '{found_path.name}': {exc}"

    if ext in _TEXT_READABLE_EXTENSIONS or found_path.stat().st_size <= 512 * 1024:
        try:
            content = found_path.read_text(encoding="utf-8", errors="replace")
            if len(content) > max_chars:
                content = content[:max_chars].rstrip() + "\n...[archivo truncado]..."
            return (
                f"Archivo leído: {found_path.name} ({_format_size(found_path.stat().st_size)})\n"
                f"Ruta: {found_path}\n\n{content or '(Archivo vacío)'}"
            )
        except Exception as exc:
            return f"Error al leer '{found_path.name}': {exc}"

    return (
        f"El archivo '{found_path.name}' tiene formato binario '{ext}' y no es texto legible directamente. "
        "Si deseas recibirlo por WhatsApp, usa `send_file_to_whatsapp`."
    )


def _find_claude_executable() -> str | None:
    """Localiza el binario/script de Claude Code CLI en Windows."""
    for candidate_name in ("claude", "claude.cmd"):
        found = shutil.which(candidate_name)
        if found:
            return found

    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")
    extra_paths = [
        os.path.join(appdata, r"npm\claude.cmd"),
        os.path.join(localappdata, r"Programs\claude\claude.exe"),
        os.path.join(userprofile, r".local\bin\claude.exe"),
    ]
    for p in extra_paths:
        if p and os.path.exists(p):
            return p
    return None


def run_claude_code_task(task_description: str, working_dir: str = "") -> str:
    """Delega una tarea de programación o automatización a Claude Code CLI (`claude -p`).

    Args:
        task_description: Instrucción clara de código, refactorización, análisis o bugfix.
        working_dir: Carpeta del proyecto donde ejecutar Claude Code (por defecto la raíz de Vortex-AI-Desktop).

    Returns:
        Resultado generado por Claude Code CLI o mensaje informativo si no está instalado.
    """
    prompt = (task_description or "").strip()
    if not prompt:
        return "Debes indicar una descripción clara de la tarea para Claude Code."

    claude_bin = _find_claude_executable()
    if not claude_bin:
        return (
            "Claude Code CLI (`claude`) no está instalado o no figura en el PATH de Windows. "
            "Instálalo con `npm install -g @anthropic-ai/claude-code` y vuelve a intentarlo."
        )

    cwd_path = BASE_DIR
    if working_dir and working_dir.strip():
        candidate_dir = Path(working_dir.strip()).expanduser()
        if candidate_dir.exists() and candidate_dir.is_dir():
            cwd_path = candidate_dir
        else:
            return f"El directorio especificado no existe: {working_dir}"

    try:
        completed = subprocess.run(
            [claude_bin, "-p", prompt],
            cwd=str(cwd_path),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return "Claude Code CLI excedió el límite de 120 segundos y fue detenido."
    except Exception as exc:
        return f"Error al ejecutar Claude Code CLI: {exc}"

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if len(stdout) > 4500:
        stdout = stdout[:4500].rstrip() + "\n...[salida truncada]..."
    if len(stderr) > 1200:
        stderr = stderr[:1200].rstrip() + "\n...[stderr truncado]..."

    parts = [f"Claude Code CLI finalizó (código {completed.returncode}) en '{cwd_path}':"]
    if stdout:
        parts.append(stdout)
    if stderr:
        parts.append(f"STDERR:\n{stderr}")
    if not stdout and not stderr:
        parts.append("(Sin salida en consola)")
    return "\n\n".join(parts)


ALL_TOOLS = [
    get_dutic_events,
    get_classroom_pending,
    get_calendar_schedule,
    create_calendar_event,
    take_screenshot,
    get_system_status,
    run_terminal_command,
    open_application_or_url,
    media_control,
    browse_and_capture,
    gui_click_and_type,
    create_google_doc_in_brave,
    ask_web_ai_in_brave,
    control_app_window,
    send_file_to_whatsapp,
    read_local_pdf_or_file,
    run_claude_code_task,
]


# ============================================================================
# 7. SEGUNDO CEREBRO (SQLITE), YOUTUBE, VIGILANTE PROACTIVO, ORGANIZADOR Y VOZ
# ============================================================================

_FOLDER_CATEGORY_MAP: dict[str, set[str]] = {
    "PDFs_Universidad": {".pdf"},
    "Comprimidos_ZIP": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "Instaladores": {".exe", ".msi", ".msix", ".bat", ".appinstaller"},
    "Imagenes_y_Capturas": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"},
    "Documentos_y_Codigo": {
        ".docx",
        ".doc",
        ".xlsx",
        ".xls",
        ".pptx",
        ".ppt",
        ".txt",
        ".md",
        ".csv",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".html",
        ".css",
        ".java",
        ".cpp",
        ".c",
        ".sql",
    },
    "Audio_y_Video": {".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".mkv", ".mov", ".avi"},
}
_SAFE_ORGANIZE_SKIP_EXTS = {".lnk", ".ini", ".tmp", ".crdownload", ".part", ".url"}


def save_to_second_brain(topic: str, content: str, tags: str = "universidad") -> str:
    """Guarda información permanente en la base de datos SQLite del Segundo Cerebro.

    Args:
        topic: Título corto o tema de la nota (ej. 'Proyecto Sistemas Operativos', 'Comandos Git').
        content: Detalle completo a recordar entre sesiones.
        tags: Etiquetas separadas por coma (por defecto 'universidad').

    Returns:
        Confirmación con el ID guardado, tema, etiquetas y fecha.
    """
    from agent_core.memory_vault import save_note

    try:
        note = save_note(topic=topic, content=content, tags=tags)
    except Exception as exc:
        return f"No se pudo guardar la nota en el Segundo Cerebro: {exc}"

    return (
        f"Nota guardada permanentemente en el Segundo Cerebro (ID #{note['id']}):\n"
        f"- Tema: {note['topic']}\n"
        f"- Etiquetas: {note['tags']}\n"
        f"- Fecha: {note['created_at']}"
    )


def search_second_brain(query: str = "") -> str:
    """Busca notas, apuntes, acuerdos o recordatorios permanentes guardados en SQLite.

    Args:
        query: Palabras clave a buscar en temas, contenido o etiquetas. Si se deja vacío,
            lista las notas más recientes almacenadas.

    Returns:
        Listado estructurado de notas encontradas en el Segundo Cerebro.
    """
    from agent_core.memory_vault import search_notes

    try:
        notes = search_notes(query=query, limit=25)
    except Exception as exc:
        return f"Error al consultar el Segundo Cerebro: {exc}"

    if not notes:
        return (
            f"No encontré notas en el Segundo Cerebro para '{query}'."
            if query.strip()
            else "El Segundo Cerebro aún no tiene notas guardadas."
        )

    header = (
        f"Resultados del Segundo Cerebro para '{query}' ({len(notes)}):"
        if query.strip()
        else f"Notas recientes en el Segundo Cerebro ({len(notes)}):"
    )
    lines = [header]
    for n in notes:
        lines.append(
            f"- [ID #{n['id']}] *{n['topic']}* ({n['tags']} | {n['created_at']}):\n  {n['content']}"
        )
    return "\n".join(lines)


def _extract_youtube_video_id(youtube_url: str) -> str:
    """Extrae el ID de 11 caracteres de enlaces youtube.com o youtu.be."""
    raw = (youtube_url or "").strip()
    if not raw:
        return ""
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw

    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        candidate = parsed.path.strip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
            return candidate
    if "youtube.com" in host or "youtube-nocookie.com" in host:
        qs = parse_qs(parsed.query)
        if "v" in qs and qs["v"]:
            candidate = qs["v"][0].strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                return candidate
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            candidate = parts[1].strip()
            if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                return candidate

    regex_match = re.search(r"(?:v=|youtu\.be/|/shorts/|/live/|/embed/)([A-Za-z0-9_-]{11})", raw)
    return regex_match.group(1) if regex_match else ""


def _format_mmss(seconds: float) -> str:
    total = max(0, int(seconds))
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    return f"{hh:02d}:{mm:02d}:{ss:02d}" if hh > 0 else f"{mm:02d}:{ss:02d}"


def _fetch_youtube_oembed(video_id: str) -> tuple[str, str]:
    try:
        resp = requests.get(
            f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
            timeout=6,
        )
        if resp.ok:
            data = resp.json()
            return str(data.get("title", "")).strip(), str(data.get("author_name", "")).strip()
    except Exception:
        pass
    return "", ""


def summarize_youtube_video(youtube_url: str) -> str:
    """Extrae la transcripción con marcas de tiempo (o metadatos vía Playwright) de un video de YouTube.

    Args:
        youtube_url: URL completa de YouTube (youtube.com/watch?v=... o youtu.be/...).

    Returns:
        Título, canal y transcripción completa con marcas de tiempo para generar apuntes o código.
    """
    video_id = _extract_youtube_video_id(youtube_url)
    if not video_id:
        return f"No pude extraer un ID válido de YouTube desde '{youtube_url}'."

    title, author = _fetch_youtube_oembed(video_id)
    meta_header = [f"Video YouTube ID: {video_id}"]
    if title:
        meta_header.append(f"Título: {title}")
    if author:
        meta_header.append(f"Canal: {author}")

    transcript_items: list[dict[str, Any]] = []
    lang_used = ""
    transcript_error = ""

    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        ytt = YouTubeTranscriptApi()
        preferred_langs = ["es", "es-419", "es-ES", "en", "en-US"]
        try:
            fetched = ytt.fetch(video_id, languages=preferred_langs)
            lang_used = getattr(fetched, "language_code", "") or "es/en"
            transcript_items = fetched.to_raw_data()
        except Exception:
            tlist = ytt.list(video_id)
            available = list(tlist)
            if available:
                first_t = available[0]
                fetched = first_t.fetch()
                lang_used = getattr(first_t, "language_code", "") or "auto"
                transcript_items = fetched.to_raw_data()
    except Exception as exc:
        transcript_error = str(exc)

    if transcript_items:
        lines: list[str] = []
        total_len = 0
        max_chars = 11000
        for item in transcript_items:
            text_seg = str(item.get("text", "")).replace("\n", " ").strip()
            if not text_seg:
                continue
            stamp = _format_mmss(float(item.get("start", 0.0)))
            row = f"[{stamp}] {text_seg}"
            if total_len + len(row) > max_chars:
                lines.append("...[transcripción truncada por longitud]...")
                break
            lines.append(row)
            total_len += len(row) + 1

        meta_header.append(f"Idioma subtítulos: {lang_used}")
        return "\n".join(meta_header) + "\n\nTranscripción:\n" + wrap_external_data("\n".join(lines))

    # Fallback con Playwright cuando el video no tiene subtítulos disponibles
    watch_url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_playwright_browse, watch_url, "", 3)
            pw_title, final_url, raw_text, _, _ = future.result(timeout=35)
        cleaned_text = re.sub(r"\s+", " ", (raw_text or "").strip())
        if len(cleaned_text) > 3500:
            cleaned_text = cleaned_text[:3500].rstrip() + "..."
        return (
            "\n".join(meta_header)
            + f"\nTítulo página: {pw_title}\nURL: {final_url}\n"
            + f"Nota: El video no expuso subtítulos ({transcript_error or 'sin transcripción'}). "
            + f"Extracto visible capturado con Playwright:\n{wrap_external_data(cleaned_text)}"
        )
    except Exception as pw_exc:
        return (
            "\n".join(meta_header)
            + f"\nNo fue posible obtener subtítulos ({transcript_error}) ni metadatos con Playwright ({pw_exc})."
        )


def set_pc_or_task_alert(
    condition_type: str,
    target_value: str,
    message: str = "",
) -> str:
    """Programa un vigilante en segundo plano que envía un mensaje proactivo a WhatsApp al cumplirse.

    Args:
        condition_type: Tipo de condición a vigilar:
            - 'timer': cuenta regresiva en minutos/segundos (ej. target_value='15', '30s', '1h', o '22:00').
            - 'process_finished': avisa apenas termine un proceso/descarga/compilación activo (ej. 'python', 'cargo').
            - 'battery_low': avisa si la batería baja del porcentaje indicado (ej. '25').
            - 'dutic_check': revisa entregas próximas en las siguientes 24h en DUTIC y envía briefing a WhatsApp.
        target_value: Valor objetivo según el tipo de condición (tiempo, nombre/PID de proceso, % batería, o 'ahora').
        message: Texto personalizado del recordatorio o alerta que llegará a WhatsApp.

    Returns:
        Confirmación con el ID de la alerta activa y su configuración.
    """
    from agent_core.watchdog import watchdog_manager

    try:
        alert = watchdog_manager.add_alert(
            condition_type=condition_type,
            target_value=target_value,
            message=message,
            dutic_fn=get_dutic_events,
            screenshot_fn=take_screenshot,
        )
    except ValueError as exc:
        return str(exc)
    except Exception as exc:
        return f"No se pudo programar la alerta proactiva: {exc}"

    return (
        f"Vigilante proactivo activado (Alerta #{alert['id']}):\n"
        f"- Tipo: {alert['condition_type']}\n"
        f"- Condición: {alert['detail']}\n"
        f"- Mensaje WhatsApp: {alert['message']}"
    )


def _resolve_user_folder(folder_name: str) -> Path | None:
    raw = (folder_name or "Downloads").strip().strip('"').strip("'")
    direct = Path(raw).expanduser()
    if direct.exists() and direct.is_dir():
        return direct.resolve()

    home = Path.home()
    key = raw.lower()
    folder_candidates = {
        "downloads": [home / "Downloads", home / "Descargas"],
        "descargas": [home / "Downloads", home / "Descargas"],
        "desktop": [home / "Desktop", home / "Escritorio"],
        "escritorio": [home / "Desktop", home / "Escritorio"],
        "documents": [home / "Documents", home / "Documentos"],
        "documentos": [home / "Documents", home / "Documentos"],
        "proyecto": [BASE_DIR],
        "project": [BASE_DIR],
        "workspace": [BASE_DIR],
        "vortex-ai-desktop": [BASE_DIR],
        "actual": [BASE_DIR],
        ".": [BASE_DIR],
    }
    for cand in folder_candidates.get(key, [home / raw]):
        if cand.exists() and cand.is_dir():
            return cand.resolve()
    return None


def _categorize_extension(ext: str) -> str:
    lower_ext = ext.lower()
    for category, exts in _FOLDER_CATEGORY_MAP.items():
        if lower_ext in exts:
            return category
    return "Otros"


def _unique_destination(dest_dir: Path, filename: str) -> Path:
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1
    while True:
        alt = dest_dir / f"{stem} ({counter}){suffix}"
        if not alt.exists():
            return alt
        counter += 1


def organize_or_inspect_folder(folder_name: str = "Downloads", mode: str = "inspect") -> str:
    """Inspecciona uso de espacio/tipos o clasifica de forma segura archivos sueltos en subcarpetas.

    Args:
        folder_name: Carpeta objetivo ('Downloads', 'Desktop', 'Documents' o ruta local).
        mode:
            - 'inspect': reporta conteo por tipo, peso acumulado y Top 10 archivos más pesados.
            - 'organize_safe': mueve archivos sueltos a subcarpetas por categoría sin borrar nada.

    Returns:
        Reporte detallado de la inspección o de los archivos organizados.
    """
    folder = _resolve_user_folder(folder_name)
    if folder is None:
        return f"No se encontró la carpeta '{folder_name}'."

    clean_mode = (mode or "inspect").strip().lower()
    if clean_mode in ("recent", "recientes", "latest") and folder_name.strip().lower() in (
        "downloads",
        "descargas",
        "",
    ):
        return list_recent_downloads(n=10)

    entries: list[tuple[Path, int, str]] = []
    skipped = 0

    try:
        for item in folder.iterdir():
            if not item.is_file():
                continue
            ext = item.suffix.lower()
            if item.name.lower() == "desktop.ini" or ext in _SAFE_ORGANIZE_SKIP_EXTS:
                skipped += 1
                continue
            try:
                size = int(item.stat().st_size)
            except OSError:
                size = 0
            entries.append((item, size, _categorize_extension(ext)))
    except OSError as exc:
        return f"No se pudo leer la carpeta '{folder}': {exc}"

    if clean_mode in ("inspect", "inspeccionar", "analyze", "analizar"):
        if not entries:
            return f"La carpeta '{folder}' no tiene archivos sueltos relevantes ({skipped} omitidos)."

        counts: dict[str, int] = {}
        sizes: dict[str, int] = {}
        total_bytes = 0
        for _, size, cat in entries:
            counts[cat] = counts.get(cat, 0) + 1
            sizes[cat] = sizes.get(cat, 0) + size
            total_bytes += size

        top_files = sorted(entries, key=lambda x: x[1], reverse=True)[:10]
        lines = [
            f"Inspección de '{folder}':",
            f"- Archivos sueltos: {len(entries)} ({_format_size(total_bytes)} en total | {skipped} accesos/temporales omitidos)",
            "\nResumen por tipo:",
        ]
        for cat in [*_FOLDER_CATEGORY_MAP.keys(), "Otros"]:
            if cat in counts:
                lines.append(f"  * {cat}: {counts[cat]} archivo(s) ({_format_size(sizes[cat])})")

        lines.append("\nTop 10 archivos más pesados:")
        for idx, (path, size, cat) in enumerate(top_files, start=1):
            lines.append(f"  {idx}. {path.name} — {_format_size(size)} [{cat}]")
        return "\n".join(lines)

    if clean_mode in ("organize_safe", "organize", "organizar", "ordenar"):
        if not entries:
            return f"No hay archivos sueltos por ordenar en '{folder}'."

        if len(entries) > 20 and should_require_confirmation():
            return request_confirmation_for_current_task(
                action_name="organize_or_inspect_folder",
                args_dict={
                    "folder_name": folder_name,
                    "mode": mode,
                    "_file_count": len(entries),
                },
                reason=f"Operación masiva sobre {len(entries)} archivos (>20)",
                task_id=get_current_task_id(),
            )

        moved_counts: dict[str, int] = {}
        errors: list[str] = []
        for src_path, _, cat in entries:
            if cat == "Otros":
                target_cat = "Otros_Archivos"
            else:
                target_cat = cat
            dest_dir = folder / target_cat
            try:
                dest_dir.mkdir(exist_ok=True)
                dest_path = _unique_destination(dest_dir, src_path.name)
                shutil.move(str(src_path), str(dest_path))
                moved_counts[target_cat] = moved_counts.get(target_cat, 0) + 1
            except Exception as exc:
                errors.append(f"{src_path.name}: {exc}")

        total_moved = sum(moved_counts.values())
        lines = [
            f"Organización segura completada en '{folder}' (sin eliminar ningún archivo):",
            f"- Archivos movidos: {total_moved} | Omitidos (.lnk/temp): {skipped}",
        ]
        for cat_name, count in moved_counts.items():
            lines.append(f"  * {cat_name}/ -> {count} archivo(s)")
        if errors:
            lines.append(f"- Con error ({len(errors)}): " + "; ".join(errors[:5]))
        return "\n".join(lines)

    return f"Modo '{mode}' no reconocido. Usa 'inspect' u 'organize_safe'."


def _read_from_clipboard() -> str:
    if pyperclip is not None:
        try:
            text = pyperclip.paste()
            if isinstance(text, str):
                return text
        except Exception:
            pass
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", "Get-Clipboard -Raw"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=5,
    )
    return completed.stdout or ""


def pc_clipboard_and_voice(action: str, text: str = "") -> str:
    """Lee/escribe el portapapeles de Windows o reproduce texto en voz alta por los parlantes.

    Args:
        action: Acción a ejecutar: 'get_clipboard', 'set_clipboard' o 'speak_aloud'.
        text: Contenido a copiar o frase a pronunciar en voz alta (requerido para set_clipboard y speak_aloud).

    Returns:
        Contenido leído del portapapeles o confirmación de copia / reproducción de voz.
    """
    act = (action or "").strip().lower()
    if act == "get_clipboard":
        try:
            clip = _read_from_clipboard()
        except Exception as exc:
            return f"Error al leer el portapapeles de Windows: {exc}"
        cleaned = clip.rstrip("\r\n")
        if not cleaned:
            return "El portapapeles de Windows está vacío o contiene datos no textuales."
        if len(cleaned) > 4000:
            cleaned = cleaned[:4000] + "\n...[portapapeles truncado]..."
        return f"Contenido actual del portapapeles de tu PC:\n\n{cleaned}"

    if act == "set_clipboard":
        if not text:
            return "Debes indicar `text` para copiarlo al portapapeles."
        try:
            _copy_to_clipboard(text)
            preview = text if len(text) <= 120 else text[:117] + "..."
            return f"Texto copiado al portapapeles de tu PC ({len(text)} caracteres): '{preview}'"
        except Exception as exc:
            return f"No se pudo escribir en el portapapeles: {exc}"

    if act == "speak_aloud":
        clean_speech = (text or "").strip()
        if not clean_speech:
            return "Debes indicar `text` para reproducirlo por voz en la PC."
        env = os.environ.copy()
        env["AGENTE_SPEECH_TEXT"] = clean_speech[:1200]
        ps_script = (
            "Add-Type -AssemblyName System.Speech; "
            "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            "$synth.Speak($env:AGENTE_SPEECH_TEXT)"
        )
        try:
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                env=env,
                capture_output=True,
                text=True,
                timeout=35,
                check=True,
            )
            return f"Mensaje reproducido en voz alta por los parlantes: '{clean_speech[:140]}'"
        except Exception as exc:
            return f"Error al reproducir voz en Windows: {exc}"

    return "Acción inválida. Usa 'get_clipboard', 'set_clipboard' o 'speak_aloud'."


# ============================================================================
# 8. FÁBRICA WEB AI, WHATSAPP A CONTACTOS, MODOS WORKSPACE, WEBCAM, GIT Y BÚSQUEDA
# ============================================================================

_HEAVY_AI_URLS: dict[str, str] = {
    "gemini": "https://gemini.google.com/app",
    "aistudio": "https://aistudio.google.com/prompts/new_chat",
    "chatgpt": "https://chatgpt.com/",
    "claude": "https://claude.ai/new",
}

_HEAVY_AI_HINTS: dict[str, list[str]] = {
    "gemini": ["Gemini", "Brave"],
    "aistudio": ["AI Studio", "Google AI Studio", "Brave"],
    "chatgpt": ["ChatGPT", "Brave"],
    "claude": ["Claude", "Brave"],
}

_BRAVE_SERVICE_URLS: dict[str, str] = {
    "sheets": "https://sheets.new",
    "google sheets": "https://sheets.new",
    "excel": "https://sheets.new",
    "hoja": "https://sheets.new",
    "hojas": "https://sheets.new",
    "slides": "https://slides.new",
    "google slides": "https://slides.new",
    "presentaciones": "https://slides.new",
    "ppt": "https://slides.new",
    "docs": "https://docs.new",
    "google docs": "https://docs.new",
    "documento": "https://docs.new",
    "github": "https://github.com",
    "facebook": "https://www.facebook.com",
    "fb": "https://www.facebook.com",
    "gmail": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "dutic": "https://dutic.unsa.edu.pe",
    "classroom": "https://classroom.google.com",
    "gemini": "https://gemini.google.com/app",
    "youtube": "https://www.youtube.com",
    "yt": "https://www.youtube.com",
}

_PROJECT_ARTIFACT_EXTS = {
    ".pptx",
    ".xlsx",
    ".pdf",
    ".py",
    ".docx",
    ".zip",
    ".csv",
    ".html",
    ".json",
    ".md",
}


def list_generated_projects(limit: int = 25) -> list[dict[str, Any]]:
    """Lista archivos creados en generated_projects/ ordenados del más reciente al más antiguo."""
    GENERATED_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    items: list[tuple[float, dict[str, Any]]] = []
    try:
        for path in GENERATED_PROJECTS_DIR.rglob("*"):
            if not path.is_file():
                continue
            ext = path.suffix.lower()
            if ext not in _PROJECT_ARTIFACT_EXTS or path.name == "artifact_spec.json":
                continue
            if "__pycache__" in path.parts:
                continue
            try:
                stat = path.stat()
                rel = path.relative_to(GENERATED_PROJECTS_DIR).as_posix()
                folder = path.parent.name if path.parent != GENERATED_PROJECTS_DIR else "raíz"
                mod_dt = datetime.fromtimestamp(stat.st_mtime, tz=LIMA_TZ).strftime(
                    "%Y-%m-%d %H:%M"
                )
                items.append(
                    (
                        stat.st_mtime,
                        {
                            "name": path.name,
                            "folder": folder,
                            "rel_path": rel,
                            "ext": ext,
                            "size_bytes": int(stat.st_size),
                            "size_label": _format_size(int(stat.st_size)),
                            "modified_at": mod_dt,
                        },
                    )
                )
            except OSError:
                continue
    except OSError:
        return []

    items.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in items[: max(1, int(limit))]]


def _normalize_target_ai(target_ai: str) -> str:
    raw = (target_ai or "gemini").strip().lower()
    if "studio" in raw or "aistudio" in raw:
        return "aistudio"
    if "gpt" in raw or "openai" in raw:
        return "chatgpt"
    if "claude" in raw or "anthropic" in raw:
        return "claude"
    return "gemini"


def _normalize_output_format(output_format: str) -> str:
    raw = (output_format or "pptx").strip().lower().lstrip(".")
    if raw in ("pptx", "ppt", "slides", "presentacion", "presentación", "diapositivas"):
        return "pptx"
    if raw in ("xlsx", "xls", "excel", "sheets", "hoja"):
        return "xlsx"
    if raw in (
        "docx",
        "doc",
        "word",
        "docs",
        "gdoc",
        "google_doc",
        "documento",
        "esquema",
        "apuntes",
    ):
        return "docx"
    if raw in ("pdf", "informe", "reporte"):
        return "pdf"
    if raw in ("py", "python", "script", "codigo", "código"):
        return "py"
    return "pptx"


def _slugify_project_name(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (text or "proyecto").strip().lower())
    cleaned = cleaned.strip("_") or "proyecto"
    return cleaned[:34]


def _build_heavy_ai_super_prompt(
    user_goal: str,
    out_fmt: str,
    pages_or_slides: int,
    output_path: Path,
) -> str:
    count = max(3, min(int(pages_or_slides), 40))

    if out_fmt == "pptx":
        return (
            f"Escribe un único script completo en Python usando python-pptx que genere una "
            f"presentación profesional y extensa de {count} diapositivas sobre: {user_goal}. "
            f"El script debe guardar el archivo exactamente en la ruta r'{output_path}'. "
            f"Pon todo dentro de un único bloque de código python sin omitir nada."
        )

    if out_fmt == "xlsx":
        return (
            f"Escribe un único script completo en Python usando openpyxl que genere un libro "
            f"Excel profesional y completo de {count} secciones/tablas sobre: {user_goal}. "
            f"El script debe guardar el archivo exactamente en la ruta r'{output_path}'. "
            f"Pon todo dentro de un único bloque de código python sin omitir nada."
        )

    if out_fmt == "docx":
        return (
            f"Escribe un único script completo y autónomo en Python usando python-docx "
            f"(`from docx import Document`) que genere un documento Word profesional (.docx) "
            f"y extenso de al menos {count} secciones principales sobre: {user_goal}. "
            f"Debe incluir portada limpia, encabezados jerárquicos con color corporativo, "
            f"párrafos amplios bien redactados, listas con viñetas, tabla resumen comparativa "
            f"y márgenes ordenados. "
            f"REGLAS ESTRICTAS: usa únicamente librería estándar + python-docx, define helpers "
            f"limpios y datos completos sin abreviar ni usar `...`, y guarda el archivo exactamente "
            f"en la ruta r'{output_path}'. Pon todo dentro de un único bloque ```python ... ```."
        )

    if out_fmt == "pdf":
        return (
            f"Escribe un único script completo en Python usando fpdf2 que genere un informe "
            f"PDF profesional y extenso de {count} páginas/secciones sobre: {user_goal}. "
            f"El script debe guardar el archivo exactamente en la ruta r'{output_path}'. "
            f"Pon todo dentro de un único bloque de código python sin omitir nada."
        )

    return (
        f"Escribe un único script completo en Python 3 sobre: {user_goal}. "
        f"El script debe ejecutarse directamente sin interacción manual y si genera archivos "
        f"guardarlos en r'{output_path.parent}'. Pon todo dentro de un único bloque de código python sin omitir nada."
    )


def _find_recent_download(since_ts: float, target_exts: set[str]) -> Path | None:
    home = Path.home()
    candidates: list[Path] = []
    for folder in (home / "Downloads", home / "Descargas"):
        if not folder.exists() or not folder.is_dir():
            continue
        try:
            for item in folder.iterdir():
                if not item.is_file():
                    continue
                ext = item.suffix.lower()
                if ext in _SKIP_SCAN_EXTENSIONS or ext not in target_exts:
                    continue
                try:
                    if item.stat().st_mtime >= since_ts - 2.0:
                        candidates.append(item)
                except OSError:
                    continue
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


_GEMINI_CODE_START_RE = re.compile(
    r"^(?:import\s+[A-Za-z_][\w.]*(?:\s*,\s*[A-Za-z_][\w.]*)*(?:\s+as\s+[A-Za-z_]\w*)?|"
    r"from\s+[A-Za-z_][\w.]*\s+import\s+.+)$"
)
_WEB_UI_STOP_PREFIXES = (
    "usa el código con precaución",
    "usa el codigo con precaucion",
    "use code with caution",
    "pregúntale a gemini",
    "preguntale a gemini",
    "ask gemini",
    "gemini puede cometer errores",
    "gemini can make mistakes",
    "chatgpt puede cometer errores",
    "chatgpt can make mistakes",
    "claude can make mistakes",
    "mostrar borradores",
    "show drafts",
    "exportar a documentos",
    "export to docs",
)
_WEB_UI_SKIP_EXACT = {
    "copiar código",
    "copiar codigo",
    "copy code",
    "copiar",
    "copy",
    "edit",
    "editar",
    "python",
    "py",
}


def _try_trim_valid_python(lines: list[str]) -> str:
    """Recorta líneas finales de ruido UI hasta obtener un módulo Python sintácticamente válido."""
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    max_pops = min(len(trimmed), 40)
    for _ in range(max_pops):
        candidate = "\n".join(trimmed).strip()
        if len(candidate) < 80:
            break
        try:
            ast.parse(candidate)
            return candidate
        except SyntaxError:
            trimmed.pop()
            while trimmed and not trimmed[-1].strip():
                trimmed.pop()
    return ""


def _extract_python_code_from_text(text: str) -> str:
    if not text or not text.strip():
        return ""
    pythonic_markers = (
        "import ",
        "from ",
        "Presentation(",
        "Workbook(",
        "FPDF(",
        ".save(",
    )
    fenced = re.findall(r"```(?:python|py)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    valid_fenced: list[str] = []
    for block in fenced:
        b = block.strip()
        if len(b) < 80 or not any(marker in b for marker in pythonic_markers):
            continue
        parsed = _try_trim_valid_python(b.splitlines())
        valid_fenced.append(parsed or b)
    if valid_fenced:
        valid_fenced.sort(key=len, reverse=True)
        return valid_fenced[0]

    # Extracción desde texto plano copiado con Ctrl+A + Ctrl+C en Gemini Web / Brave
    lines = text.splitlines()
    candidates: list[str] = []
    for start_idx, raw_line in enumerate(lines):
        if not _GEMINI_CODE_START_RE.match(raw_line.strip()):
            continue
        collected: list[str] = []
        for line in lines[start_idx:]:
            low = line.strip().lower()
            if any(low.startswith(prefix) for prefix in _WEB_UI_STOP_PREFIXES):
                break
            if low in _WEB_UI_SKIP_EXACT:
                continue
            collected.append(line)

        parsed_code = _try_trim_valid_python(collected)
        if parsed_code:
            candidates.append(parsed_code)
            continue

        raw_candidate = "\n".join(collected).strip()
        if len(raw_candidate) >= 120 and any(
            tok in raw_candidate for tok in ("Presentation(", "Workbook(", "FPDF(", ".save(")
        ):
            candidates.append(raw_candidate)

    if candidates:
        artifact_candidates = [
            c
            for c in candidates
            if any(tok in c for tok in ("pptx", "Presentation", "openpyxl", "Workbook", "fpdf", ".save("))
        ]
        pool = artifact_candidates or candidates
        pool.sort(key=len, reverse=True)
        return pool[0]
    return ""


def _run_generated_python_script(script_path: Path, output_path: Path) -> tuple[bool, Path, str]:
    venv_py = BASE_DIR / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_py) if venv_py.exists() else sys.executable

    env = os.environ.copy()
    env["OUTPUT_FILE_PATH"] = str(output_path)
    env["PYTHONUTF8"] = "1"
    try:
        completed = subprocess.run(
            [python_exe, str(script_path)],
            cwd=str(script_path.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=65,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, output_path, f"Excepción al ejecutar script: {exc}"

    out_log = ((completed.stdout or "") + "\n" + (completed.stderr or "")).strip()
    if output_path.exists() and output_path.stat().st_size > 0:
        return True, output_path, out_log

    # Si el script guardó con otro nombre en project_dir, localizar el artefacto creado
    target_ext = output_path.suffix.lower()
    generated = [
        p
        for p in script_path.parent.iterdir()
        if p.is_file() and p.suffix.lower() == target_ext and p != script_path
    ]
    if generated:
        generated.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return True, generated[0], out_log

    return False, output_path, f"Código salida {completed.returncode}: {out_log[:600]}"


def _focus_brave_and_get_rect(hints: list[str]) -> tuple[bool, str, tuple[int, int, int, int]]:
    """Enfoca/maximiza Brave con _force_focus_window y retorna su rectángulo vía GetWindowRect."""
    focused, win_title = _focus_window_by_keywords(hints, maximize=True)
    if not focused:
        focused, win_title = _focus_window_by_keywords(["Brave"], maximize=True)

    hwnd, found_title = _find_window_handle(hints)
    if not hwnd:
        hwnd, found_title = _find_window_handle(["Brave"])
    if found_title and not win_title:
        win_title = found_title

    if hwnd:
        _force_focus_window(hwnd, maximize=True)
        if win32gui is not None:
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                w = max(200, int(r) - int(l))
                h = max(200, int(b) - int(t))
                return focused or True, win_title or "Brave", (int(l), int(t), w, h)
            except Exception:
                pass
        rect = _RECT()
        if ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            w = max(200, rect.right - rect.left)
            h = max(200, rect.bottom - rect.top)
            return focused or True, win_title or "Brave", (rect.left, rect.top, w, h)

    screen_w, screen_h = pyautogui.size()
    return focused, win_title or "Brave", (0, 0, int(screen_w), int(screen_h))


def _safe_copy_brave_page_text(window_hints: list[str]) -> str:
    """Copia el contenido completo de la conversación en Brave bajo GUI_LOCK."""
    with GUI_LOCK:
        focused, _, (left, top, w, h) = _focus_brave_and_get_rect(window_hints)
        if not focused:
            return ""
        _copy_to_clipboard("")
        time.sleep(0.3)
        pyautogui.FAILSAFE = False
        # Clic en el área central de la respuesta de Gemini Web dentro de Brave
        pyautogui.click(left + int(w * 0.50), top + int(h * 0.50))
        time.sleep(0.25)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.25)
        pyautogui.hotkey("ctrl", "c")
        time.sleep(0.45)
        pyautogui.press("right")
        return _read_from_clipboard()


def _parse_ai_sections(user_goal: str, raw_text: str, count: int) -> list[dict[str, Any]]:
    """Convierte texto extraído de la IA Web (o estructura base del tema) en secciones limpias."""
    noise_tokens = {
        "new chat",
        "nuevo chat",
        "gemini",
        "chatgpt",
        "claude",
        "google ai studio",
        "ask anything",
        "pregunta a gemini",
        "iniciar sesión",
        "sign in",
        "copy code",
        "copiar código",
    }
    clean_lines: list[str] = []
    for line in (raw_text or "").splitlines():
        s = re.sub(r"\s+", " ", line).strip()
        if len(s) < 12 or s.lower() in noise_tokens:
            continue
        if s.startswith("http") and " " not in s:
            continue
        if s not in clean_lines:
            clean_lines.append(s)

    target_slides = max(3, min(int(count), 20))
    sections: list[dict[str, Any]] = []

    if len(clean_lines) >= 6:
        chunk_size = max(3, min(5, len(clean_lines) // max(1, target_slides - 2)))
        idx = 0
        sec_num = 1
        while idx < len(clean_lines) and len(sections) < target_slides - 2:
            chunk = clean_lines[idx : idx + chunk_size]
            idx += chunk_size
            heading = chunk[0][:75]
            bullets = chunk[1:] if len(chunk) > 1 else [chunk[0]]
            sections.append(
                {
                    "title": f"{sec_num}. {heading}",
                    "bullets": [b[:180] for b in bullets],
                }
            )
            sec_num += 1

    # Completar hasta alcanzar las secciones solicitadas con análisis estructurado del tema
    template_topics = [
        (
            "Fundamentos y Contexto",
            [
                f"Definición central y alcance práctico aplicado a: {user_goal}.",
                "Problema arquitectónico o académico que resuelve y motivación principal.",
                "Conceptos clave y terminología estándar utilizada en producción.",
            ],
        ),
        (
            "Componentes y Responsabilidades",
            [
                "Separación clara de responsabilidades entre módulos e interfaces.",
                "Contrato de datos entre la capa de entrada, lógica y persistencia.",
                "Ventajas de mantenibilidad, testabilidad y evolución independiente.",
            ],
        ),
        (
            "Flujo de Ejecución Paso a Paso",
            [
                "1) Recepción de evento o solicitud inicial del usuario/sistema.",
                "2) Validación, orquestación de reglas de negocio y estado.",
                "3) Generación de salida, actualización visual y retroalimentación.",
            ],
        ),
        (
            "Buenas Prácticas y Patrones Asociados",
            [
                "Bajo acoplamiento y alta cohesión mediante inyección de dependencias.",
                "Manejo explícito de errores, validaciones en frontera y logs trazables.",
                "Estrategias de pruebas unitarias e integración por componente.",
            ],
        ),
        (
            "Errores Frecuentes y Mitigación",
            [
                "Evitar sobrecargar una única capa con lógica de negocio y presentación.",
                "Prevenir duplicación de estado y acoplamiento rígido a detalles externos.",
                "Documentar decisiones críticas y criterios de aceptación medibles.",
            ],
        ),
        (
            "Conclusiones y Siguientes Pasos",
            [
                f"Síntesis de implementación recomendada para {user_goal}.",
                "Checklist de verificación antes de pasar a entorno productivo o entrega.",
                "Referencias para profundización y mejoras iterativas.",
            ],
        ),
    ]

    for t_title, t_bullets in template_topics:
        if len(sections) >= target_slides - 1:
            break
        sections.append({"title": f"{len(sections) + 1}. {t_title}", "bullets": t_bullets})

    return sections


def _build_fallback_pptx_script(spec_path: Path) -> str:
    return f'''import json
import os
from pathlib import Path
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

spec = json.loads(Path(r"{spec_path}").read_text(encoding="utf-8"))
output_path = Path(os.environ.get("OUTPUT_FILE_PATH", spec["output_path"]))
output_path.parent.mkdir(parents=True, exist_ok=True)

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
blank = prs.slide_layouts[6]

BG = RGBColor(15, 23, 42)
PANEL = RGBColor(23, 35, 58)
ACCENT = RGBColor(56, 189, 248)
TEXT = RGBColor(241, 245, 249)
MUTED = RGBColor(148, 163, 184)

def paint_bg(slide):
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = BG
    bar = slide.shapes.add_shape(1, Inches(0), Inches(0), Inches(13.333), Inches(0.12))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()

# Portada
s0 = prs.slides.add_slide(blank)
paint_bg(s0)
tb0 = s0.shapes.add_textbox(Inches(1.0), Inches(2.0), Inches(11.3), Inches(2.4))
tf0 = tb0.text_frame
tf0.word_wrap = True
p0 = tf0.paragraphs[0]
p0.text = spec["goal"][:140]
p0.font.size = Pt(38)
p0.font.bold = True
p0.font.color.rgb = TEXT

p_sub = tf0.add_paragraph()
p_sub.text = f"Generado con Fábrica Vortex AI Desktop · IA: {{spec['target_ai'].upper()}} · {{spec['created_at']}}"
p_sub.font.size = Pt(16)
p_sub.font.color.rgb = ACCENT
p_sub.space_before = Pt(18)

sections = spec["sections"]
target_total = max(3, int(spec["pages_or_slides"]))
content_limit = max(1, target_total - 2)

for idx, sec in enumerate(sections[:content_limit], start=1):
    slide = prs.slides.add_slide(blank)
    paint_bg(slide)

    card = slide.shapes.add_shape(1, Inches(0.75), Inches(1.45), Inches(11.83), Inches(5.35))
    card.fill.solid()
    card.fill.fore_color.rgb = PANEL
    card.line.color.rgb = RGBColor(51, 65, 85)

    tbox = slide.shapes.add_textbox(Inches(0.75), Inches(0.42), Inches(11.83), Inches(0.9))
    ttf = tbox.text_frame
    ttf.word_wrap = True
    tp = ttf.paragraphs[0]
    tp.text = sec["title"]
    tp.font.size = Pt(26)
    tp.font.bold = True
    tp.font.color.rgb = TEXT

    bbox = slide.shapes.add_textbox(Inches(1.1), Inches(1.75), Inches(11.1), Inches(4.7))
    btf = bbox.text_frame
    btf.word_wrap = True
    for b_idx, bullet in enumerate(sec["bullets"]):
        p = btf.paragraphs[0] if b_idx == 0 else btf.add_paragraph()
        p.text = f"• {{bullet}}"
        p.font.size = Pt(18)
        p.font.color.rgb = TEXT
        p.space_after = Pt(14)

    fbox = slide.shapes.add_textbox(Inches(0.75), Inches(6.95), Inches(11.8), Inches(0.4))
    fp = fbox.text_frame.paragraphs[0]
    fp.text = f"Vortex AI Desktop | Diapositiva {{idx + 1}} de {{target_total}}"
    fp.font.size = Pt(11)
    fp.font.color.rgb = MUTED

# Diapositiva final con tabla resumen
s_end = prs.slides.add_slide(blank)
paint_bg(s_end)
etitle = s_end.shapes.add_textbox(Inches(0.75), Inches(0.42), Inches(11.8), Inches(0.8))
ep = etitle.text_frame.paragraphs[0]
ep.text = "Resumen Estructural y Puntos Clave"
ep.font.size = Pt(26)
ep.font.bold = True
ep.font.color.rgb = TEXT

rows = min(len(sections) + 1, 7)
tbl = s_end.shapes.add_table(rows, 2, Inches(0.75), Inches(1.45), Inches(11.8), Inches(5.1)).table
tbl.columns[0].width = Inches(4.2)
tbl.columns[1].width = Inches(7.6)
tbl.cell(0, 0).text = "Sección"
tbl.cell(0, 1).text = "Idea Principal"
for r_i in range(1, rows):
    sec = sections[r_i - 1]
    tbl.cell(r_i, 0).text = sec["title"][:60]
    tbl.cell(r_i, 1).text = (sec["bullets"][0] if sec["bullets"] else "")[:150]

prs.save(str(output_path))
print(f"SUCCESS: {{output_path}}")
'''


def _build_fallback_xlsx_script(spec_path: Path) -> str:
    return f'''import json
import os
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

spec = json.loads(Path(r"{spec_path}").read_text(encoding="utf-8"))
output_path = Path(os.environ.get("OUTPUT_FILE_PATH", spec["output_path"]))
output_path.parent.mkdir(parents=True, exist_ok=True)

wb = Workbook()
ws = wb.active
ws.title = "Resumen"
header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
header_font = Font(color="FFFFFF", bold=True, size=12)

ws.append(["Proyecto / Tema", spec["goal"]])
ws.append(["IA Origen", spec["target_ai"].upper()])
ws.append(["Fecha (Lima)", spec["created_at"]])
ws.append([])
ws.append(["N°", "Sección", "Detalle Principal", "Puntos Clave"])

for cell in ws[5]:
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = Alignment(horizontal="center", vertical="center")

for idx, sec in enumerate(spec["sections"], start=1):
    bullets = sec["bullets"]
    ws.append([idx, sec["title"], bullets[0] if bullets else "", " | ".join(bullets[1:])])

ws2 = wb.create_sheet("Checklist")
ws2.append(["Item", "Estado", "Responsable"])
for cell in ws2[1]:
    cell.fill = header_fill
    cell.font = header_font
for sec in spec["sections"]:
    ws2.append([sec["title"], "Pendiente revisión", "Vortex AI Desktop"])

for sheet in (ws, ws2):
    for col in sheet.columns:
        max_len = max((len(str(c.value or "")) for c in col), default=12)
        sheet.column_dimensions[col[0].column_letter].width = min(max(max_len + 4, 14), 56)

wb.save(str(output_path))
print(f"SUCCESS: {{output_path}}")
'''


def _build_fallback_pdf_script(spec_path: Path) -> str:
    return f'''import json
import os
from pathlib import Path
from fpdf import FPDF
from fpdf.enums import XPos, YPos

spec = json.loads(Path(r"{spec_path}").read_text(encoding="utf-8"))
output_path = Path(os.environ.get("OUTPUT_FILE_PATH", spec["output_path"]))
output_path.parent.mkdir(parents=True, exist_ok=True)

def safe_latin(s):
    return str(s or "").encode("latin-1", "replace").decode("latin-1")

pdf = FPDF()
pdf.set_auto_page_break(auto=True, margin=15)
pdf.add_page()
pdf.set_font("Helvetica", "B", 18)
pdf.set_x(pdf.l_margin)
pdf.multi_cell(pdf.epw, 10, safe_latin(spec["goal"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
pdf.ln(3)
pdf.set_font("Helvetica", "", 10)
pdf.set_x(pdf.l_margin)
pdf.cell(
    pdf.epw,
    8,
    safe_latin(f"Vortex AI Desktop | IA: {{spec['target_ai'].upper()}} | {{spec['created_at']}}"),
    new_x=XPos.LMARGIN,
    new_y=YPos.NEXT,
)
pdf.ln(5)

for sec in spec["sections"]:
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(pdf.epw, 8, safe_latin(sec["title"]), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font("Helvetica", "", 11)
    for b in sec["bullets"]:
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(pdf.epw, 6, safe_latin(f"- {{b}}"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

pdf.output(str(output_path))
print(f"SUCCESS: {{output_path}}")
'''


def _build_fallback_docx_script(spec_path: Path) -> str:
    return f'''import json
import os
from pathlib import Path
from docx import Document
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls
from docx.shared import Inches, Pt, RGBColor

spec = json.loads(Path(r"{spec_path}").read_text(encoding="utf-8"))
output_path = Path(os.environ.get("OUTPUT_FILE_PATH", spec["output_path"]))
output_path.parent.mkdir(parents=True, exist_ok=True)

doc = Document()
for sec_layout in doc.sections:
    sec_layout.top_margin = Inches(0.85)
    sec_layout.bottom_margin = Inches(0.85)
    sec_layout.left_margin = Inches(0.9)
    sec_layout.right_margin = Inches(0.9)

navy = RGBColor(30, 58, 138)
slate = RGBColor(51, 65, 85)
body_color = RGBColor(30, 41, 59)

normal_style = doc.styles["Normal"]
normal_style.font.name = "Calibri"
normal_style.font.size = Pt(11)
normal_style.font.color.rgb = body_color

title_p = doc.add_paragraph()
title_run = title_p.add_run(str(spec.get("goal") or "Documento Técnico").strip())
title_run.bold = True
title_run.font.size = Pt(22)
title_run.font.color.rgb = navy

meta_p = doc.add_paragraph()
meta_run = meta_p.add_run(
    f"Vortex AI Desktop  |  Origen IA: {{str(spec.get('target_ai', 'gemini')).upper()}}  |  Fecha: {{spec.get('created_at', '')}}"
)
meta_run.font.size = Pt(10)
meta_run.font.color.rgb = slate

intro_p = doc.add_paragraph(
    f"Documento estructurado automáticamente sobre: {{spec.get('goal', '')}}. "
    "Incluye desarrollo por secciones, puntos clave y tabla resumen."
)
intro_p.paragraph_format.space_after = Pt(8)

sections = list(spec.get("sections") or [])
while len(sections) < 3:
    idx_n = len(sections) + 1
    sections.append({{
        "title": f"Sección Complementaria {{idx_n}}",
        "bullets": [f"Desarrollo de contexto y lineamientos clave para {{spec.get('goal', 'el tema')}}."],
    }})

for idx, sec in enumerate(sections, start=1):
    h = doc.add_heading(f"{{idx}}. {{sec.get('title', 'Sección')}}", level=1)
    for r in h.runs:
        r.font.color.rgb = navy
        r.font.size = Pt(14)

    bullets = [str(b).strip() for b in (sec.get("bullets") or []) if str(b).strip()]
    if bullets:
        lead = doc.add_paragraph(bullets[0])
        lead.paragraph_format.space_after = Pt(4)
        for b in bullets[1:] if len(bullets) > 1 else bullets:
            bp = doc.add_paragraph(b, style="List Bullet")
            bp.paragraph_format.space_after = Pt(2)
    else:
        doc.add_paragraph(f"Análisis detallado correspondiente a {{sec.get('title', 'la sección')}}.")

sum_h = doc.add_heading("Tabla Resumen de Contenidos", level=1)
for r in sum_h.runs:
    r.font.color.rgb = navy
    r.font.size = Pt(14)

table = doc.add_table(rows=1, cols=3)
table.style = "Table Grid"
headers = ["N°", "Sección / Eje", "Idea Principal"]
for col_i, text in enumerate(headers):
    cell = table.rows[0].cells[col_i]
    cell._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {{nsdecls("w")}} w:fill="1E3A8A"/>'))
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(10)
    run.font.color.rgb = RGBColor(255, 255, 255)

for idx, sec in enumerate(sections, start=1):
    row_cells = table.add_row().cells
    bullets = sec.get("bullets") or [""]
    row_cells[0].text = str(idx)
    row_cells[1].text = str(sec.get("title") or "")[:90]
    row_cells[2].text = str(bullets[0] if bullets else "")[:180]

doc.save(str(output_path))
print(f"SUCCESS: {{output_path}}")
'''


def _compile_fallback_artifact(
    out_fmt: str,
    user_goal: str,
    target_ai: str,
    raw_ai_text: str,
    pages_or_slides: int,
    project_dir: Path,
    artifact_path: Path,
) -> tuple[bool, Path, str]:
    """Genera y ejecuta un script autónomo de respaldo garantizado en project_dir."""
    project_dir.mkdir(parents=True, exist_ok=True)
    if out_fmt == "py":
        extracted = _extract_python_code_from_text(raw_ai_text)
        code_body = extracted or (
            f'"""Proyecto generado automáticamente: {user_goal}"""\n\n'
            "def main() -> None:\n"
            f"    print('Proyecto listo: {user_goal.replace(chr(39), '')}')\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        artifact_path.write_text(code_body, encoding="utf-8")
        ok, out_file, log = _run_generated_python_script(artifact_path, artifact_path)
        return ok or artifact_path.exists(), out_file, log

    import json

    sections = _parse_ai_sections(user_goal, raw_ai_text, pages_or_slides)
    spec_path = project_dir / "artifact_spec.json"
    spec_data = {
        "goal": user_goal,
        "target_ai": target_ai,
        "output_path": str(artifact_path),
        "pages_or_slides": int(pages_or_slides),
        "created_at": datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M"),
        "sections": sections,
    }
    spec_path.write_text(json.dumps(spec_data, ensure_ascii=False, indent=2), encoding="utf-8")

    builders = {
        "pptx": _build_fallback_pptx_script,
        "xlsx": _build_fallback_xlsx_script,
        "docx": _build_fallback_docx_script,
        "pdf": _build_fallback_pdf_script,
    }
    script_code = builders.get(out_fmt, _build_fallback_pptx_script)(spec_path)
    script_path = project_dir / "generator_script.py"
    script_path.write_text(script_code, encoding="utf-8")
    return _run_generated_python_script(script_path, artifact_path)


def _generate_script_with_gemini_api(
    user_goal: str,
    out_fmt: str,
    slides_count: int,
    artifact_path: Path,
) -> tuple[str, str, str]:
    """Auto-fallback infalible con genai.Client y GEMINI_MODELS_COMPLEX cuando el clipboard web falla."""
    api_key = get_env("GEMINI_API_KEY")
    if not api_key or api_key == "TU_GEMINI_API_KEY_AQUI":
        return "", "", "GEMINI_API_KEY no configurada para fallback"

    lib_spec = {
        "pptx": (
            f"python-pptx en formato panorámico 16:9 (13.333 x 7.5 pulgadas), "
            f"paleta corporativa elegante (azul oscuro #0F172A, cian #38BDF8, gris pizarra), "
            f"portada profesional y exactamente {slides_count} diapositivas con tarjetas, bullets sustanciales y tabla resumen"
        ),
        "xlsx": (
            f"openpyxl con múltiples hojas, encabezados con relleno corporativo, "
            f"{slides_count} secciones/filas detalladas, ajuste de ancho de columnas y fórmulas reales"
        ),
        "docx": (
            f"python-docx con portada elegante en azul marino (#1E3A8A), márgenes limpios, "
            f"encabezados jerárquicos, al menos {slides_count} secciones bien redactadas con párrafos "
            f"y viñetas reales, además de una tabla resumen estilizada"
        ),
        "pdf": (
            f"fpdf2 con portada limpia, tipografía clara, {slides_count} secciones detalladas y manejo seguro de acentos"
        ),
        "py": "Python 3 estándar completo y autónomo con pruebas básicas y main()",
    }.get(out_fmt, "python-pptx")

    prompt = (
        f"Eres un Arquitecto de Software Senior. Genera UN ÚNICO bloque de código ```python ... ``` "
        f"completo, autónomo y 100% ejecutable en Windows.\n"
        f"Tema: {user_goal}\n"
        f"Requisito técnico ({out_fmt.upper()}): usa {lib_spec}.\n"
        f"Guarda el resultado exactamente en `Path(os.environ.get('OUTPUT_FILE_PATH', r'{artifact_path}'))` "
        f"(creando su carpeta padre si falta) y sin usar caracteres que fallen en consola Windows."
    )

    dedup_models: list[str] = []
    for m in [*GEMINI_MODELS_COMPLEX, GEMINI_MODEL]:
        if m and m not in dedup_models:
            dedup_models.append(m)
    dedup_models = filter_available_models(dedup_models)
    if not dedup_models:
        return "", "", "Todos los modelos complejos están en cooldown o deshabilitados (404)"

    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
    last_err = ""
    for model_name in dedup_models:
        try:
            resp = client.models.generate_content(model=model_name, contents=prompt)
            raw_text = (resp.text or "").strip()
            code = _extract_python_code_from_text(raw_text)
            if code:
                clear_model_cooldown(model_name)
                return code, model_name, raw_text
            last_err = f"{model_name}: respuesta sin bloque Python válido"
        except Exception as exc:
            err_str = str(exc)
            classify_and_record_model_error(model_name, err_str)
            last_err = f"{model_name}: {err_str}"
            continue

    return "", "", last_err


def _find_recent_download_stable(since_ts: float, target_exts: set[str]) -> Path | None:
    """Busca un archivo recién descargado (sin .crdownload/.tmp) y verifica tamaño estable 2s."""
    home = Path.home()
    candidates: list[Path] = []
    for folder in (home / "Downloads", home / "Descargas"):
        if not folder.exists() or not folder.is_dir():
            continue
        try:
            for item in folder.iterdir():
                if not item.is_file():
                    continue
                name_low = item.name.lower()
                if name_low.endswith((".crdownload", ".tmp", ".part")):
                    continue
                ext = item.suffix.lower()
                if ext in _SKIP_SCAN_EXTENSIONS or ext not in target_exts:
                    continue
                try:
                    if item.stat().st_mtime >= since_ts - 2.0:
                        candidates.append(item)
                except OSError:
                    continue
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    chosen = candidates[0]
    try:
        size_before = chosen.stat().st_size
        if size_before <= 0:
            return None
        time.sleep(2.0)
        if not chosen.exists():
            return None
        size_after = chosen.stat().st_size
        return chosen if size_after > 0 and size_after == size_before else None
    except OSError:
        return None


def _compute_image_mean_diff(img_a: Image.Image, img_b: Image.Image) -> float:
    """Calcula la diferencia media RGB local entre dos capturas sin gastar tokens API."""
    a_rgb = img_a.convert("RGB")
    b_rgb = img_b.convert("RGB")
    if a_rgb.size != b_rgb.size:
        b_rgb = b_rgb.resize(a_rgb.size)
    diff = ImageChops.difference(a_rgb, b_rgb)
    stat = ImageStat.Stat(diff)
    return sum(stat.mean) / max(1, len(stat.mean))


def _ensure_docx_has_content(
    docx_path: Path,
    target_sections: int,
    user_goal: str,
    raw_ai_text: str,
) -> tuple[int, str]:
    """Valida que un .docx generado tenga contenido sustancial (>= 5 párrafos) y completa si faltan."""
    try:
        from docx import Document
    except Exception:
        return 0, "python-docx no disponible para validar documento"

    try:
        doc = Document(str(docx_path))
        non_empty = [p for p in doc.paragraphs if (p.text or "").strip()]
        actual = len(non_empty)
        min_required = max(5, int(target_sections))
        if actual >= min_required:
            return actual, f"{actual} párrafos verificados (mínimo {min_required})"

        sections = _parse_ai_sections(user_goal, raw_ai_text, max(3, int(target_sections)))
        sec_idx = 0
        while len([p for p in doc.paragraphs if (p.text or "").strip()]) < min_required:
            sec = (
                sections[sec_idx % len(sections)]
                if sections
                else {
                    "title": f"Sección Complementaria {sec_idx + 1}",
                    "bullets": [f"Desarrollo técnico aplicado a: {user_goal}."],
                }
            )
            sec_idx += 1
            doc.add_heading(sec["title"][:90], level=1)
            for bullet in (sec.get("bullets") or [user_goal])[:4]:
                doc.add_paragraph(str(bullet), style="List Bullet")

        doc.save(str(docx_path))
        final_count = len([p for p in doc.paragraphs if (p.text or "").strip()])
        return final_count, f"{actual} -> {final_count} párrafos verificados y completados"
    except Exception as exc:
        return 0, f"No se pudo validar .docx ({exc})"


def _ensure_pptx_has_all_slides(
    pptx_path: Path,
    expected_slides: int,
    user_goal: str,
    raw_ai_text: str,
) -> tuple[int, str]:
    """Valida que un .pptx tenga al menos todas las diapositivas solicitadas y completa si faltan."""
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt
    except Exception:
        return 0, "python-pptx no disponible para validar diapositivas"

    try:
        prs = Presentation(str(pptx_path))
        actual = len(prs.slides)
        target = max(3, int(expected_slides))
        if actual >= target:
            return actual, f"{actual}/{target} diapositivas verificadas"

        sections = _parse_ai_sections(user_goal, raw_ai_text, target)
        blank = prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]
        bg_color = RGBColor(15, 23, 42)
        panel_color = RGBColor(23, 35, 58)
        accent_color = RGBColor(56, 189, 248)
        text_color = RGBColor(241, 245, 249)

        sec_idx = 0
        while len(prs.slides) < target:
            slide = prs.slides.add_slide(blank)
            bg = slide.background.fill
            bg.solid()
            bg.fore_color.rgb = bg_color
            bar = slide.shapes.add_shape(1, Inches(0), Inches(0), prs.slide_width, Inches(0.1))
            bar.fill.solid()
            bar.fill.fore_color.rgb = accent_color
            bar.line.fill.background()

            sec = sections[sec_idx % len(sections)] if sections else {
                "title": f"Sección Complementaria {len(prs.slides) + 1}",
                "bullets": [f"Análisis detallado aplicado a: {user_goal}."],
            }
            sec_idx += 1

            card = slide.shapes.add_shape(1, Inches(0.75), Inches(1.45), Inches(11.8), Inches(5.3))
            card.fill.solid()
            card.fill.fore_color.rgb = panel_color
            card.line.color.rgb = RGBColor(51, 65, 85)

            tbox = slide.shapes.add_textbox(Inches(0.75), Inches(0.42), Inches(11.8), Inches(0.9))
            tp = tbox.text_frame.paragraphs[0]
            tp.text = f"{len(prs.slides)}. {sec['title'][:75]}"
            tp.font.size = Pt(26)
            tp.font.bold = True
            tp.font.color.rgb = text_color

            bbox = slide.shapes.add_textbox(Inches(1.1), Inches(1.75), Inches(11.1), Inches(4.6))
            btf = bbox.text_frame
            btf.word_wrap = True
            for b_i, bullet in enumerate(sec.get("bullets") or [user_goal]):
                bp = btf.paragraphs[0] if b_i == 0 else btf.add_paragraph()
                bp.text = f"• {bullet}"
                bp.font.size = Pt(18)
                bp.font.color.rgb = text_color
                bp.space_after = Pt(12)

        prs.save(str(pptx_path))
        new_count = len(prs.slides)
        return new_count, f"Validado y completado a {new_count}/{target} diapositivas (tenía {actual})"
    except Exception as exc:
        return 0, f"No se pudo validar conteo de diapositivas: {exc}"


def build_with_heavy_web_ai(
    user_goal: str,
    target_ai: str = "gemini",
    output_format: str = "pptx",
    pages_or_slides: int = 8,
    wait_seconds: int = 600,
) -> str:
    """Abre Gemini Web/IA Web en Brave real usando Visión AI y Pixel-Diff inteligente (hasta 600s).

    Args:
        user_goal: Objetivo o tema completo (ej. 'diapositivas sobre Patrón MVC de 8 páginas').
        target_ai: IA web destino en Brave: 'gemini', 'aistudio', 'chatgpt' o 'claude'.
        output_format: Formato final deseado: 'pptx', 'xlsx', 'docx', 'pdf' o 'py'.
        pages_or_slides: Cantidad de diapositivas, hojas o secciones esperadas (por defecto 8).
        wait_seconds: Tiempo máximo inteligente de espera en IA Web (20 a 600s, por defecto 600).

    Returns:
        Resumen con ruta del artefacto final compilado, validación y captura DPI-aware para WhatsApp.
    """
    goal = (user_goal or "").strip()
    if not goal:
        return "Debes indicar `user_goal` describiendo qué archivo o proyecto quieres construir."

    ai_key = _normalize_target_ai(target_ai)
    out_fmt = _normalize_output_format(output_format)
    max_wait_sec = max(20, min(int(wait_seconds), 600))
    slides_count = max(3, min(int(pages_or_slides), 40))

    GENERATED_PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(LIMA_TZ).strftime("%Y%m%d_%H%M%S")
    project_dir = GENERATED_PROJECTS_DIR / f"{stamp}_{_slugify_project_name(goal)}"
    project_dir.mkdir(parents=True, exist_ok=True)

    artifact_path = project_dir / f"output.{out_fmt}"
    build_script_path = project_dir / ("output.py" if out_fmt == "py" else "build_artifact.py")
    super_prompt = _build_heavy_ai_super_prompt(goal, out_fmt, slides_count, artifact_path)
    (project_dir / "super_prompt.txt").write_text(super_prompt, encoding="utf-8")

    started_ts = time.time()
    url = _HEAVY_AI_URLS[ai_key]
    hints = _HEAVY_AI_HINTS[ai_key]

    # 1. Abrir Brave real y enfocar ventana; esperar 5s de carga inicial
    brave_focused = False
    win_title = "Brave"
    brave_mon_idx = 0
    _dyn_mons = _get_monitors_dynamic()
    mon_rect: dict[str, int] = (
        dict(_dyn_mons[0]["mss_rect"])
        if _dyn_mons
        else {"left": 0, "top": 0, "width": 1920, "height": 1080}
    )
    with GUI_LOCK:
        brave_focused = _launch_and_focus_brave(
            url,
            wait_seconds=5.0,
            new_window=False,
            maximize=True,
            focus_hints=hints,
        )
        focused, win_title, (left, top, win_w, win_h) = _focus_brave_and_get_rect(hints)
        brave_focused = brave_focused or focused
        mon_rect = {"left": left, "top": top, "width": win_w, "height": win_h}

    cap_init = capture_monitor_image(0, target_window_title="Brave", enqueue=False)
    if cap_init.get("ok"):
        brave_mon_idx = int(cap_init.get("monitor_index") or 0)
        if cap_init.get("monitor"):
            mon_rect = dict(cap_init["monitor"])

    # 2. Ubicación visual real de la caja de texto + verificación Ctrl+A/Ctrl+C antes de Enter
    input_x = int(mon_rect.get("left", 0)) + int(int(mon_rect.get("width", 1920)) * 0.50)
    input_y = int(mon_rect.get("top", 0)) + int(int(mon_rect.get("height", 1080)) * 0.88)
    prompt_verified = False
    prompt_prefix = super_prompt[:28].strip()

    for attempt in range(2):
        loc = _vision_locate_on_screen(
            brave_mon_idx,
            (
                "caja de texto principal donde dice Pregunta a Gemini / Ask Gemini / Enter a prompt / "
                "Message ChatGPT para escribir el prompt. Si hay un popup o banner bloqueando la pantalla, "
                "devuelve status='popup' con las coordenadas del botón Cerrar o X."
            ),
            target_window_title="Brave",
        )
        if loc.get("ok") and loc.get("status") == "popup" and loc.get("found"):
            with GUI_LOCK:
                _focus_brave_and_get_rect(hints)
                pyautogui.click(int(loc["real_x"]), int(loc["real_y"]))
            time.sleep(1.0)
            loc = _vision_locate_on_screen(
                brave_mon_idx,
                "caja de texto donde dice Pregunta a Gemini / Ask Gemini / Enter a prompt para escribir el prompt",
                target_window_title="Brave",
            )

        if loc.get("ok") and loc.get("found"):
            input_x = int(loc["real_x"])
            input_y = int(loc["real_y"])

        with GUI_LOCK:
            _focus_brave_and_get_rect(hints)
            pyautogui.click(input_x, input_y)
            time.sleep(0.35)
            _copy_to_clipboard(super_prompt)
            time.sleep(0.35)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.4)

            _copy_to_clipboard("")
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.2)
            pyautogui.hotkey("ctrl", "c")
            time.sleep(0.3)
            clipboard_val = _read_from_clipboard().strip()

            if clipboard_val and (
                clipboard_val.startswith(prompt_prefix) or prompt_prefix in clipboard_val
            ):
                pyautogui.press("right")
                time.sleep(0.15)
                pyautogui.press("enter")
                prompt_verified = True
                break

            if attempt == 1:
                pyautogui.press("right")
                time.sleep(0.15)
                pyautogui.press("enter")

    # 3. Espera inteligente de hasta max_wait_sec (600s) con Pixel-Diff local (0 API mientras cambia)
    time.sleep(2.0)
    prev_cap = capture_monitor_image(brave_mon_idx, target_window_title="Brave", enqueue=False)
    prev_img: Image.Image | None = None
    if prev_cap.get("ok") and Path(str(prev_cap["path"])).exists():
        try:
            with Image.open(str(prev_cap["path"])) as im_prev:
                prev_img = im_prev.convert("RGB")
        except Exception:
            prev_img = None

    compilation_mode = ""
    raw_copied_text = ""
    extracted_code = ""
    final_artifact: Path | None = None
    exec_log = ""
    used_script_path = build_script_path
    target_download_exts = {f".{out_fmt}", ".pptx", ".xlsx", ".pdf", ".docx", ".zip", ".py"}

    wait_start = time.monotonic()
    unchanged_sec = 0
    vision_checks = 0

    while (time.monotonic() - wait_start) < max_wait_sec:
        if is_task_cancelled():
            exec_log = f"{exec_log} | Cancelado por timeout/señal de tarea".strip(" |")
            break
        time.sleep(5.0)
        if is_task_cancelled():
            exec_log = f"{exec_log} | Cancelado por timeout/señal de tarea".strip(" |")
            break
        elapsed_wait = time.monotonic() - wait_start

        # a) Revisar si apareció archivo nuevo en Downloads con tamaño estable 2s
        downloaded = _find_recent_download_stable(started_ts, target_download_exts)
        if downloaded is not None:
            dest_dl = project_dir / downloaded.name
            try:
                shutil.copy2(str(downloaded), str(dest_dl))
                final_artifact = dest_dl
                compilation_mode = f"Descarga directa en Downloads ({downloaded.name})"
                break
            except OSError:
                final_artifact = None

        # b) Calcular Pixel-Diff local (0 tokens API)
        curr_cap = capture_monitor_image(brave_mon_idx, target_window_title="Brave", enqueue=False)
        curr_img: Image.Image | None = None
        if curr_cap.get("ok") and Path(str(curr_cap["path"])).exists():
            try:
                with Image.open(str(curr_cap["path"])) as im_curr:
                    curr_img = im_curr.convert("RGB")
            except Exception:
                curr_img = None

        if prev_img is not None and curr_img is not None:
            mean_diff = _compute_image_mean_diff(prev_img, curr_img)
            prev_img = curr_img
            # c) Mientras la pantalla siga cambiando, esperar gratis en silencio
            if mean_diff > 0.8:
                unchanged_sec = 0
                continue
            unchanged_sec += 5
        else:
            if curr_img is not None:
                prev_img = curr_img
            unchanged_sec += 5

        # d) Solo cuando lleve 15s consecutivos sin cambios tras >=20s iniciales, consultar a Vision
        if elapsed_wait >= 20.0 and unchanged_sec >= 15:
            vision_checks += 1
            vis_status = _vision_locate_on_screen(
                brave_mon_idx,
                (
                    "Mira la pantalla de Gemini Web: ¿Sigue generando (hay botón Detener/Stop o animación de carga) "
                    "o ya terminó? Si ya terminó, ¿hay algún botón de Descargar/Exportar o un botón de Copiar código? "
                    "Devuelve JSON con status ('generating' | 'finished_download' | 'finished_code' | 'error') "
                    "y las coordenadas [x, y] (0-1000) del botón de Descargar o Copiar código si existe."
                ),
                target_window_title="Brave",
            )
            st = vis_status.get("status", "")

            # e) Si sigue generando, seguir esperando con Pixel-Diff
            if st == "generating":
                unchanged_sec = 0
                continue

            # f) Si hay botón Descargar/Exportar
            if st == "finished_download" and vis_status.get("found"):
                with GUI_LOCK:
                    _focus_brave_and_get_rect(hints)
                    pyautogui.click(int(vis_status["real_x"]), int(vis_status["real_y"]))
                for _ in range(8):
                    time.sleep(2.0)
                    downloaded = _find_recent_download_stable(started_ts, target_download_exts)
                    if downloaded is not None:
                        dest_dl = project_dir / downloaded.name
                        try:
                            shutil.copy2(str(downloaded), str(dest_dl))
                            final_artifact = dest_dl
                            compilation_mode = (
                                f"Descarga directa desde botón Exportar/Descargar en {ai_key.upper()} Web"
                            )
                            break
                        except OSError:
                            final_artifact = None
                if final_artifact is not None:
                    break

            # g) Si terminó con bloque de código Python
            if st in ("finished_code", "ready"):
                if vis_status.get("found"):
                    with GUI_LOCK:
                        _focus_brave_and_get_rect(hints)
                        _copy_to_clipboard("")
                        time.sleep(0.2)
                        pyautogui.click(int(vis_status["real_x"]), int(vis_status["real_y"]))
                        time.sleep(0.6)
                    raw_copied_text = _read_from_clipboard()
                    extracted_code = _extract_python_code_from_text(raw_copied_text)

                if not extracted_code:
                    page_text = _safe_copy_brave_page_text(hints)
                    if len(page_text) > len(raw_copied_text):
                        raw_copied_text = page_text
                    extracted_code = _extract_python_code_from_text(raw_copied_text)

                if raw_copied_text:
                    (project_dir / "web_ai_response.txt").write_text(raw_copied_text, encoding="utf-8")

                if extracted_code:
                    ast_hits = scan_python_ast_for_danger(extracted_code)
                    if ast_hits:
                        ast_warn = (
                            f"Script web descartado por escáner AST (llamadas peligrosas: {', '.join(ast_hits)}). "
                            "Se usó el generador local determinista."
                        )
                        exec_log = f"{exec_log} | {ast_warn}".strip(" |")
                        append_backend_log(f"[ASTGuard] {ast_warn}")
                        break

                    build_script_path.write_text(extracted_code, encoding="utf-8")
                    if out_fmt == "py":
                        final_artifact = build_script_path
                        compilation_mode = f"Script Python real extraído de {ai_key.upper()} Web en Brave"
                        _, _, exec_log = _run_generated_python_script(build_script_path, build_script_path)
                        break
                    ok, built_file, exec_log = _run_generated_python_script(build_script_path, artifact_path)
                    if ok and built_file.exists():
                        final_artifact = built_file
                        compilation_mode = (
                            f"Código real de {ai_key.upper()} Web en Brave compilado en {build_script_path.name}"
                        )
                        break

            if st in ("error", "not_found") or vision_checks >= 4:
                break

    # 4. Último recurso con _generate_script_with_gemini_api (GEMINI_MODELS_COMPLEX) si Web AI falló
    if (final_artifact is None or not final_artifact.exists()) and "escáner AST" not in exec_log:
        api_code, api_model, api_note = _generate_script_with_gemini_api(
            user_goal=goal,
            out_fmt=out_fmt,
            slides_count=slides_count,
            artifact_path=artifact_path,
        )
        if api_code:
            build_script_path.write_text(api_code, encoding="utf-8")
            used_script_path = build_script_path
            if out_fmt == "py":
                final_artifact = build_script_path
                compilation_mode = f"Último recurso API complejo ({api_model}) en build_artifact.py"
                _, _, exec_log = _run_generated_python_script(build_script_path, build_script_path)
            else:
                ok, built_file, exec_log = _run_generated_python_script(build_script_path, artifact_path)
                if ok and built_file.exists():
                    final_artifact = built_file
                    compilation_mode = (
                        f"Último recurso API complejo ({api_model}) compilado en {build_script_path.name}"
                    )
        elif api_note:
            exec_log = f"{exec_log} | API fallback: {api_note}".strip(" |")

    # 5. Garantía estructural si también falló la API al compilar
    if final_artifact is None or not final_artifact.exists():
        ok, built_file, fb_log = _compile_fallback_artifact(
            out_fmt=out_fmt,
            user_goal=goal,
            target_ai=ai_key,
            raw_ai_text=raw_copied_text,
            pages_or_slides=slides_count,
            project_dir=project_dir,
            artifact_path=artifact_path,
        )
        if ok and built_file.exists():
            final_artifact = built_file
            used_script_path = project_dir / "generator_script.py"
            compilation_mode = "Respaldo estructural local garantizado tras ciclo Brave"
        else:
            exec_log = f"{exec_log} | {fb_log}".strip(" |")

    slide_validation_note = ""
    docx_validation_note = ""
    if final_artifact is not None and final_artifact.exists() and out_fmt == "pptx":
        _, slide_validation_note = _ensure_pptx_has_all_slides(
            final_artifact, slides_count, goal, raw_copied_text
        )
    elif final_artifact is not None and final_artifact.exists() and out_fmt == "docx":
        _, docx_validation_note = _ensure_docx_has_content(
            final_artifact, slides_count, goal, raw_copied_text
        )

    # 6. Regla estricta de entregables: SOLO archivo final en PENDING_FILES y 1 sola captura limpia DPI-aware
    pop_pending_files()
    pop_pending_images()
    final_cap = capture_monitor_image(brave_mon_idx, target_window_title="Brave", enqueue=True)
    final_screenshot_note = (
        f"Captura limpia ({final_cap['label']} | {final_cap['width']}x{final_cap['height']}) -> {final_cap['path']}"
        if final_cap.get("ok")
        else str(final_cap.get("error", "No se pudo capturar pantalla"))
    )

    if final_artifact is None or not final_artifact.exists():
        return (
            f"Se abrió {ai_key.upper()} Web en Brave ({win_title}, input {input_x},{input_y}), "
            f"pero no se pudo compilar el artefacto en '{project_dir}'.\n"
            f"- Detalle: {exec_log}\n"
            f"- Captura final: {final_screenshot_note}"
        )

    _enqueue_file(final_artifact)

    summary_lines = [
        f"Artefacto generado en '{project_dir.name}' (Brave: {win_title}):",
        f"- Archivo final: {final_artifact} ({_format_size(final_artifact.stat().st_size)})",
        f"- Método: {compilation_mode}",
        f"- Script interno guardado en PC: {used_script_path}",
        f"- Prompt verificado antes de Enter: {'Sí' if prompt_verified else 'Reintentado'} (clic {input_x},{input_y})",
    ]
    if slide_validation_note:
        summary_lines.append(f"- Validación PPTX: {slide_validation_note}")
    if docx_validation_note:
        summary_lines.append(f"- Validación DOCX: {docx_validation_note}")
    summary_lines.append(f"- Captura final Brave: {final_screenshot_note}")
    summary_lines.append(
        "Solo el entregable final y una única captura limpia quedaron adjuntos para WhatsApp."
    )
    return "\n".join(summary_lines)


def send_whatsapp_to_contact(contact_name_or_number: str, message_text: str) -> str:
    """Envía un mensaje de WhatsApp real a un contacto por nombre o número vía el puente Baileys local (sin abrir navegador).

    Args:
        contact_name_or_number: Nombre guardado en WhatsApp (ej. 'Papá', 'Juan Perez') o número telefónico.
        message_text: Texto exacto del mensaje a enviar al contacto bajo orden explícita del usuario.

    Returns:
        Confirmación con el contacto encontrado y JID destino, o detalle del error si no se halló.
    """
    contact = (contact_name_or_number or "").strip()
    msg = (message_text or "").strip()
    if not contact:
        return "Debes indicar el nombre o número del contacto destinatario."
    if not msg:
        return "El texto del mensaje a enviar no puede estar vacío."

    if should_require_confirmation():
        return request_confirmation_for_current_task(
            action_name="send_whatsapp_to_contact",
            args_dict={"contact_name_or_number": contact, "message_text": msg},
            reason="Envío de WhatsApp a un tercero",
            task_id=get_current_task_id(),
        )

    has_letters = bool(re.search(r"[A-Za-zÀ-ÿ]", contact))
    digits_only = re.sub(r"\D", "", contact)
    if not has_letters and len(digits_only) >= 8:
        if len(digits_only) == 9 and digits_only.startswith("9"):
            digits_only = f"51{digits_only}"
        contact = digits_only

    notify_port = get_env("WHATSAPP_NOTIFY_PORT", "8001") or "8001"
    endpoint = f"http://127.0.0.1:{notify_port}/send-contact"
    try:
        resp = requests.post(
            endpoint,
            json={"contact": contact, "message": msg},
            timeout=20,
        )
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        if resp.ok and data.get("ok"):
            matched = data.get("matched_name") or contact
            jid = data.get("jid") or ""
            return (
                f"WhatsApp enviado correctamente a *{matched}* ({jid}):\n"
                f"\"{msg}\""
            )
        err_msg = data.get("error") or f"HTTP {resp.status_code}: {resp.text[:240]}"
        known = data.get("known_contacts")
        suggestions = data.get("suggestions") or []
        parts = [f"No se pudo enviar el WhatsApp a '{contact}': {err_msg}"]
        if known is not None:
            parts.append(f"(Contactos indexados actualmente: {known})")
        if suggestions:
            sug_items = [
                (
                    f"{s.get('name', '?')} ({s.get('phone') or s.get('jid', '')})"
                    if isinstance(s, dict)
                    else str(s)
                )
                for s in suggestions[:5]
                if s
            ]
            if sug_items:
                parts.append(f"Contactos parecidos: {', '.join(sug_items)}.")
        parts.append(
            "REGLA ESTRICTA: NO ejecutes 'node -e' en consola ni abras 'wa.me' o WhatsApp Web en Brave. "
            "Informa al usuario estas coincidencias o pídele su número telefónico directo."
        )
        return " ".join(parts)
    except Exception as exc:
        return f"Error al comunicar con el puente de WhatsApp ({endpoint}): {exc}"


def _search_youtube_first_video_id(query: str) -> str:
    """Extrae el primer videoId real de 11 caracteres desde YouTube Search para autoplay directo."""
    clean_q = (query or "").strip()
    if not clean_q:
        return ""
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(clean_q)}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "es-PE,es;q=0.9,en;q=0.8",
    }
    try:
        resp = requests.get(search_url, headers=headers, timeout=6)
        if resp.ok:
            for vid in re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text):
                if vid.lower() != "results" and len(vid) == 11:
                    return vid
    except Exception:
        pass
    return ""


def open_and_automate_brave_service(
    service_or_url: str,
    data_to_paste: str = "",
    shortcut_after_load: str = "",
    send_to_whatsapp: bool = False,
) -> str:
    """Abre YouTube (autoplay del primer video) o servicios web en Brave real y toma 1 captura limpia.

    Args:
        service_or_url: Nombre del servicio ('youtube', 'sheets', 'slides', 'docs', 'github', 'facebook', 'drive', 'gmail'),
            una búsqueda directa de YouTube (ej. 'youtube: la burbuja de la ia'), o URL directa.
        data_to_paste: Búsqueda de YouTube (si el servicio es 'youtube') o texto/tabla tabulada para pegar en Sheets/Docs.
        shortcut_after_load: Atajo de teclado opcional después de cargar (ej. 'ctrl+s', 'f11').
        send_to_whatsapp: Si True, encola la captura de Brave para enviarla por WhatsApp; por defecto False (solo visión interna).

    Returns:
        Confirmación de la ventana real de Brave abierta y captura de pantalla.
    """
    raw = (service_or_url or "").strip()
    if not raw:
        return "Debes indicar el servicio o URL a abrir en Brave."

    key = raw.lower()
    paste_payload = data_to_paste
    focus_hints = ["- Brave", "Brave"]
    youtube_query = ""
    is_youtube_request = False

    wants_results_only = any(
        tok in f"{key} {(data_to_paste or '').lower()}"
        for tok in ("solo resultados", "ver resultados", "mostrar resultados", "lista de resultados")
    )

    if key in ("youtube", "yt"):
        is_youtube_request = True
        youtube_query = (data_to_paste or "").strip()
        paste_payload = ""
        focus_hints = ["YouTube", "- Brave", "Brave"]
    elif key.startswith(("youtube:", "youtube ", "yt:", "yt ", "buscar en youtube ")):
        is_youtube_request = True
        for prefix in ("buscar en youtube ", "youtube:", "youtube ", "yt:", "yt "):
            if key.startswith(prefix):
                youtube_query = raw[len(prefix) :].strip()
                break
        if not youtube_query:
            youtube_query = (data_to_paste or "").strip()
        paste_payload = ""
        focus_hints = ["YouTube", "- Brave", "Brave"]
    elif "youtube.com/watch" in key and ("v=results" in key or "v=results&" in key):
        is_youtube_request = True
        parsed_yt = urlparse(raw)
        qs_yt = parse_qs(parsed_yt.query)
        youtube_query = (qs_yt.get("search_query", [""])[0] or data_to_paste or "").strip()
        paste_payload = ""
        focus_hints = ["YouTube", "- Brave", "Brave"]
    elif "youtube.com/results" in key:
        is_youtube_request = True
        parsed_yt = urlparse(raw)
        qs_yt = parse_qs(parsed_yt.query)
        youtube_query = (qs_yt.get("search_query", [""])[0] or data_to_paste or "").strip()
        paste_payload = ""
        focus_hints = ["YouTube", "- Brave", "Brave"]

    yt_video_id = ""
    opened_results_with_query = False
    if is_youtube_request:
        if youtube_query and not wants_results_only:
            yt_video_id = _search_youtube_first_video_id(youtube_query)
            if yt_video_id:
                target_url = f"https://www.youtube.com/watch?v={yt_video_id}&autoplay=1"
            else:
                target_url = f"https://www.youtube.com/results?search_query={quote_plus(youtube_query)}"
                opened_results_with_query = True
        elif youtube_query:
            target_url = f"https://www.youtube.com/results?search_query={quote_plus(youtube_query)}"
        else:
            target_url = "https://www.youtube.com"
    elif key in _BRAVE_SERVICE_URLS:
        target_url = _BRAVE_SERVICE_URLS[key]
    elif key.startswith(("http://", "https://")):
        target_url = raw
        if "youtube.com" in key or "youtu.be" in key:
            focus_hints = ["YouTube", "- Brave", "Brave"]
    elif " " not in key and "." in key:
        target_url = f"https://{raw}"
    else:
        target_url = f"https://www.google.com/search?q={quote_plus(raw)}"

    actions_log: list[str] = []
    focused = False
    try:
        with GUI_LOCK:
            focused = _launch_and_focus_brave(
                target_url,
                wait_seconds=2.5,
                new_window=False,
                maximize=True,
                focus_hints=focus_hints,
            )

            if paste_payload:
                _copy_to_clipboard(paste_payload)
                time.sleep(0.25)
                pyautogui.hotkey("ctrl", "v")
                actions_log.append(f"Datos pegados ({len(paste_payload)} caracteres)")

            if shortcut_after_load and shortcut_after_load.strip():
                keys = [k.strip().lower() for k in shortcut_after_load.split("+") if k.strip()]
                if keys:
                    time.sleep(0.25)
                    pyautogui.hotkey(*keys)
                    actions_log.append(f"Atajo ejecutado: {'+'.join(keys)}")
    except Exception as exc:
        return f"No se pudo abrir '{target_url}' en Brave: {exc}"

    if opened_results_with_query and not wants_results_only:
        time.sleep(1.2)
        vis_thumb = _vision_locate_on_screen(
            0,
            f"miniatura o título del primer video relevante de YouTube para '{youtube_query}' para hacer clic y reproducirlo",
            target_window_title="Brave",
        )
        if vis_thumb.get("ok") and vis_thumb.get("found"):
            with GUI_LOCK:
                _focus_brave_and_get_rect(focus_hints)
                pyautogui.click(int(vis_thumb["real_x"]), int(vis_thumb["real_y"]))
            actions_log.append(
                f"Clic visual en primer video ({vis_thumb['real_x']}, {vis_thumb['real_y']})"
            )

    time.sleep(0.4)
    if send_to_whatsapp:
        pop_pending_images()
    screenshot_status = take_screenshot(
        target_window_title="Brave",
        send_to_whatsapp=send_to_whatsapp,
    )
    focus_label = "Brave real enfocado" if focused else "Brave lanzado en sesión del usuario"
    if yt_video_id:
        actions_log.insert(
            0,
            f"Video YouTube abierto con autoplay: '{youtube_query}' (videoId={yt_video_id})",
        )
    elif youtube_query:
        actions_log.insert(0, f"Búsqueda en YouTube con tu cuenta activa: '{youtube_query}'")
    ops = " | ".join(actions_log) if actions_log else "Página abierta en Brave real con tu sesión"
    return (
        f"Servicio abierto en Brave ({focus_label}): {target_url}\n"
        f"- Acciones: {ops}\n"
        f"- {screenshot_status}"
    )


def _move_window_to_monitor(hwnd: int, monitor: dict[str, Any], press_f11: bool = False) -> bool:
    """Mueve y dimensiona una ventana Win32 hacia las coordenadas de un monitor físico."""
    if not hwnd:
        return False
    user32 = ctypes.windll.user32
    left = int(monitor.get("left", 0))
    top = int(monitor.get("top", 0))
    width = max(800, int(monitor.get("width", 1920)))
    height = max(600, int(monitor.get("height", 1080)))

    user32.ShowWindow(hwnd, 9)  # SW_RESTORE para poder mover si estaba maximizada en otro monitor
    time.sleep(0.15)
    user32.MoveWindow(hwnd, left, top, width, height, True)
    time.sleep(0.15)
    _force_focus_window(hwnd, maximize=not press_f11)

    if press_f11:
        time.sleep(0.25)
        pyautogui.press("f11")
    return True


def _minimize_distractor_windows() -> int:
    """Minimiza ventanas de distracción comunes (navegador, chat, multimedia) conservando editores."""
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    distractors = (
        "brave",
        "chrome",
        "firefox",
        "edge",
        "spotify",
        "discord",
        "whatsapp",
        "telegram",
        "youtube",
        "netflix",
        "twitch",
        "steam",
    )
    keep_tokens = ("antigravity", "visual studio code", "vortex")
    minimized = 0

    def _cb(hwnd: int, _lparam: int) -> bool:
        nonlocal minimized
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_title(hwnd).lower()
        if not title or any(k in title for k in keep_tokens):
            return True
        if any(d in title for d in distractors):
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            minimized += 1
        return True

    user32.EnumWindows(EnumWindowsProc(_cb), 0)
    return minimized


def activate_workspace_mode(mode_name: str, send_to_whatsapp: bool = False) -> str:
    """Activa con 1 sola orden un entorno completo de trabajo en la PC y los monitores.

    Args:
        mode_name: Modo deseado:
            - 'universidad' / 'estudio': abre DUTIC UNSA, Classroom y Gemini en Brave y enfoca Antigravity/VS Code.
            - 'cine' / 'tv': abre Brave, lo mueve a las coordenadas del Monitor 2 (TV) y activa pantalla completa F11.
            - 'concentracion': minimiza ventanas distractoras y enfoca el editor de código principal.
        send_to_whatsapp: Si True, encola la captura de confirmación para WhatsApp; por defecto False.

    Returns:
        Resumen del modo activado y captura de confirmación.
    """
    mode = (mode_name or "").strip().lower()

    if mode in ("universidad", "estudio", "study", "unsa"):
        urls = [
            "https://dutic.unsa.edu.pe",
            "https://classroom.google.com",
            "https://gemini.google.com/app",
        ]
        for idx, u in enumerate(urls):
            _open_url_in_brave(u, new_window=(idx == 0))
            time.sleep(0.45)
        time.sleep(3.5)
        with GUI_LOCK:
            focused, win_title = _focus_window_by_keywords(
                ["Antigravity", "Visual Studio Code", "Code"], maximize=True
            )
            if not focused:
                focused, win_title = _focus_window_by_keywords(["- Brave", "Brave"], maximize=True)
        shot = take_screenshot(
            target_window_title=win_title or "Brave",
            send_to_whatsapp=send_to_whatsapp,
        )
        return (
            f"Modo *Estudio* activado: DUTIC, Classroom y Gemini abiertos en Brave; "
            f"ventana enfocada: {win_title or 'Brave'}.\n{shot}"
        )

    if mode in ("cine", "tv", "pelicula", "película", "multimedia"):
        monitors = get_monitors_metadata()
        if not monitors:
            return "No se pudieron consultar los monitores conectados mediante mss."
        target_mon_idx = 2 if len(monitors) >= 2 else 1
        target_mon = monitors[target_mon_idx - 1]

        hwnd, win_title = _find_window_handle(["- Brave", "Brave"])
        if not hwnd:
            with GUI_LOCK:
                _launch_and_focus_brave("https://www.youtube.com", wait_seconds=4.0)
            hwnd, win_title = _find_window_handle(["- Brave", "Brave"])

        if not hwnd:
            return "Se abrió Brave, pero no se encontró su handle de ventana para moverlo a la TV."

        with GUI_LOCK:
            _move_window_to_monitor(hwnd, target_mon, press_f11=True)

        time.sleep(1.2)
        shot = take_screenshot(
            monitor_number=target_mon_idx,
            send_to_whatsapp=send_to_whatsapp,
        )
        return (
            f"Modo *Cine / TV* activado en {target_mon['name']} "
            f"({target_mon['width']}x{target_mon['height']}, pos {target_mon['left']},{target_mon['top']}): "
            f"Brave movido y puesto en pantalla completa (F11).\n{shot}"
        )

    if mode in ("concentracion", "concentración", "focus", "deep", "programar"):
        with GUI_LOCK:
            minimized = _minimize_distractor_windows()
            time.sleep(0.25)
            focused, win_title = _focus_window_by_keywords(
                ["Antigravity", "Visual Studio Code", "Code"], maximize=True
            )
        shot = take_screenshot(
            target_window_title=win_title or "Antigravity",
            send_to_whatsapp=send_to_whatsapp,
        )
        return (
            f"Modo *Concentración* activado: {minimized} ventana(s) distractora(s) minimizada(s); "
            f"editor enfocado: {win_title or 'Editor'}.\n{shot}"
        )

    return (
        f"Modo '{mode_name}' no reconocido. "
        "Usa 'universidad' (o 'estudio'), 'cine' (o 'tv') o 'concentracion'."
    )


def webcam_security_snapshot(send_to_whatsapp: bool = True) -> str:
    """Toma una foto instantánea con la webcam de la PC, apaga la cámara al instante y envía la foto por WhatsApp."""
    try:
        import cv2
    except ImportError as exc:
        return f"Falta opencv-python en .venv para usar la webcam: {exc}"

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    out_path = TEMP_DIR / f"webcam_{int(time.time() * 1000)}.png"

    cap = None
    try:
        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return "No se pudo abrir ninguna webcam conectada (índice 0)."

        frame = None
        for _ in range(6):
            ok, grabbed = cap.read()
            if ok and grabbed is not None:
                frame = grabbed
            time.sleep(0.05)

        if frame is None:
            return "La webcam se abrió pero no entregó ningún cuadro de imagen válido."

        if not cv2.imwrite(str(out_path), frame):
            return f"No se pudo guardar la imagen de webcam en '{out_path}'."
    except Exception as exc:
        return f"Error al capturar desde la webcam: {exc}"
    finally:
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass

    record_internal_screenshot(out_path)
    if send_to_whatsapp:
        _enqueue_image(out_path)
    note = "y encolada para enviarse por WhatsApp." if send_to_whatsapp else "(solo captura interna de visión)."
    return (
        f"Foto instantánea tomada con la webcam (cámara liberada) en '{out_path}' {note}"
    )


def git_project_manager(
    repo_path: str = "",
    action: str = "status",
    commit_message: str = "",
) -> str:
    """Gestiona repositorios Git locales (status, commit, push, pull, log, diff).

    Args:
        repo_path: Ruta al repositorio Git local (si se omite, usa la raíz de Vortex-AI-Desktop).
        action: Operación Git: 'status', 'commit', 'push', 'pull', 'log' o 'diff'.
        commit_message: Mensaje del commit (obligatorio cuando `action='commit'`).

    Returns:
        Resultado de los comandos Git ejecutados en el repositorio.
    """
    target_dir = Path(repo_path.strip()).expanduser() if (repo_path or "").strip() else BASE_DIR
    if not target_dir.exists() or not target_dir.is_dir():
        return f"El directorio del repositorio no existe: {target_dir}"

    act = (action or "status").strip().lower()

    def _run_git(args: list[str]) -> tuple[int, str]:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        out = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
        return proc.returncode, out

    try:
        rc_check, out_check = _run_git(["rev-parse", "--is-inside-work-tree"])
        if rc_check != 0 or out_check.strip().lower() != "true":
            return f"'{target_dir}' no es un repositorio Git válido: {out_check}"

        if act == "status":
            _, st = _run_git(["status", "-sb"])
            _, br = _run_git(["log", "-n", "3", "--oneline"])
            return f"Git Status en '{target_dir}':\n{st}\n\nÚltimos commits:\n{br}"

        if act == "diff":
            _, df = _run_git(["diff", "--stat"])
            return f"Git Diff --stat en '{target_dir}':\n{df or '(Sin cambios pendientes sin confirmar)'}"

        if act == "log":
            _, lg = _run_git(["log", "-n", "10", "--oneline"])
            return f"Historial reciente en '{target_dir}':\n{lg}"

        if act == "pull":
            rc, out = _run_git(["pull"])
            return f"Git pull (código {rc}) en '{target_dir}':\n{out or '(Completado)'}"

        if act == "commit":
            msg = (commit_message or "").strip()
            if not msg:
                return "Debes proporcionar `commit_message` para ejecutar `git commit`."
            rc_add, out_add = _run_git(["add", "-A"])
            if rc_add != 0:
                return f"Error en `git add -A`: {out_add}"
            rc_cm, out_cm = _run_git(["commit", "-m", msg])
            return f"Git commit (código {rc_cm}) en '{target_dir}':\n{out_cm}"

        if act == "push":
            rc, out = _run_git(["push"])
            return f"Git push (código {rc}) en '{target_dir}':\n{out or '(Completado)'}"

        if act in ("reset --hard", "reset_hard", "clean", "clean -fd", "push --force", "push_force", "push -f"):
            if should_require_confirmation():
                return request_confirmation_for_current_task(
                    action_name="git_project_manager",
                    args_dict={
                        "repo_path": str(target_dir),
                        "action": act,
                        "commit_message": commit_message,
                    },
                    reason=f"Operación Git destructiva ({act})",
                    task_id=get_current_task_id(),
                )
            if act in ("reset --hard", "reset_hard"):
                rc, out = _run_git(["reset", "--hard"])
            elif act in ("clean", "clean -fd"):
                rc, out = _run_git(["clean", "-fd"])
            else:
                rc, out = _run_git(["push", "--force"])
            return f"Git {act} (código {rc}) en '{target_dir}':\n{out or '(Completado)'}"

        return f"Acción Git '{action}' no reconocida. Usa: status, commit, push, pull, log o diff."
    except subprocess.TimeoutExpired:
        return f"El comando Git en '{target_dir}' superó el límite de 30 segundos."
    except Exception as exc:
        return f"Error al ejecutar Git en '{target_dir}': {exc}"


def deep_search_in_files(search_term: str) -> str:
    """Busca una palabra o frase dentro del contenido de PDFs y archivos de código/texto en Desktop, Downloads, Documents y generated_projects.

    Args:
        search_term: Palabra o frase exacta (sin distinción de mayúsculas) a buscar dentro de los archivos.

    Returns:
        Lista de archivos coincidentes con la ruta y el extracto donde aparece el término.
    """
    term = (search_term or "").strip()
    if not term:
        return "Debes indicar `search_term` para buscar dentro de tus archivos."

    term_lower = term.lower()
    home = Path.home()
    roots = [
        BASE_DIR,
        home / "Desktop",
        home / "Escritorio",
        home / "Downloads",
        home / "Descargas",
        home / "Documents",
        home / "Documentos",
        DOCS_BACKUP_DIR,
        GENERATED_PROJECTS_DIR,
    ]
    unique_roots: list[Path] = []
    for r in roots:
        if r.exists() and r.is_dir() and r not in unique_roots:
            unique_roots.append(r)

    matches: list[str] = []
    scanned_files = 0
    deadline = time.monotonic() + 18.0

    for root in unique_roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline or len(matches) >= 20 or scanned_files >= 450:
                break
            dirnames[:] = [d for d in dirnames if d not in _SKIP_SCAN_DIRS]
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            if rel_depth >= 3:
                dirnames[:] = []

            for fname in filenames:
                if time.monotonic() > deadline or len(matches) >= 20 or scanned_files >= 450:
                    break
                fpath = Path(dirpath) / fname
                ext = fpath.suffix.lower()
                if ext in _SKIP_SCAN_EXTENSIONS:
                    continue
                if ext != ".pdf" and ext not in _TEXT_READABLE_EXTENSIONS:
                    continue

                scanned_files += 1
                if term_lower in fname.lower():
                    matches.append(f"- [Nombre] {fpath}")
                    continue

                try:
                    size_bytes = fpath.stat().st_size
                except OSError:
                    continue
                if size_bytes <= 0 or size_bytes > 12 * 1024 * 1024:
                    continue

                if ext == ".pdf":
                    try:
                        from pypdf import PdfReader

                        reader = PdfReader(str(fpath))
                        for p_idx, page in enumerate(reader.pages[:30], start=1):
                            text = page.extract_text() or ""
                            pos = text.lower().find(term_lower)
                            if pos != -1:
                                snippet = re.sub(
                                    r"\s+", " ", text[max(0, pos - 60) : pos + 140]
                                ).strip()
                                matches.append(
                                    f"- [PDF p.{p_idx}] {fpath}\n  «{snippet}»"
                                )
                                break
                    except Exception:
                        continue
                elif size_bytes <= 2 * 1024 * 1024:
                    try:
                        content = fpath.read_text(encoding="utf-8", errors="ignore")
                        pos = content.lower().find(term_lower)
                        if pos != -1:
                            snippet = re.sub(
                                r"\s+", " ", content[max(0, pos - 60) : pos + 140]
                            ).strip()
                            matches.append(f"- [{ext or 'Archivo'}] {fpath}\n  «{snippet}»")
                    except OSError:
                        continue

    if not matches:
        return (
            f"No encontré '{term}' en ninguno de los {scanned_files} archivos (PDF/código/texto) "
            "escaneados en Desktop, Downloads, Documents y generated_projects."
        )

    return (
        f"Búsqueda profunda de '{term}' ({len(matches)} coincidencia(s) en {scanned_files} archivos revisados):\n"
        + "\n".join(matches)
    )


ALL_TOOLS.extend(
    [
        save_to_second_brain,
        search_second_brain,
        summarize_youtube_video,
        set_pc_or_task_alert,
        organize_or_inspect_folder,
        pc_clipboard_and_voice,
        build_with_heavy_web_ai,
        send_whatsapp_to_contact,
        open_and_automate_brave_service,
        activate_workspace_mode,
        webcam_security_snapshot,
        git_project_manager,
        deep_search_in_files,
        *DRIVE_AND_FILE_TOOLS,
    ]
)

for _wh_fn in (
    send_whatsapp_to_contact,
    run_terminal_command,
    git_project_manager,
    organize_or_inspect_folder,
    read_local_pdf_or_file,
    send_file_to_whatsapp,
    analyze_pdf_with_gemini,
):
    register_action_handler(_wh_fn.__name__, _wh_fn)
