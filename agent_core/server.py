"""Servidor HTTP local FastAPI y Dashboard Cockpit para el Asistente UNSA."""

from __future__ import annotations

import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import base64
import json
import os
import re
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from agent_core.agent import UniversityPCAgent
from agent_core.config import (
    BASE_DIR,
    DEAD_MODELS,
    DISCARDED_MODELS,
    GEMINI_MODEL,
    GEMINI_MODELS_COMPLEX,
    GEMINI_MODELS_FAST,
    LIMA_TZ,
    MODEL_CAPABILITIES,
    TEMP_DIR,
    get_env,
    get_model_validation_status,
)
from agent_core.memory_vault import delete_note, save_note, search_notes
from agent_core.security import cleanup_old_confirmations
from agent_core.tools import (
    GENERATED_PROJECTS_DIR,
    _get_monitors_dynamic,
    capture_monitor_image,
    get_default_monitor_mode,
    get_dutic_events,
    list_generated_projects,
    set_default_monitor_mode,
    take_screenshot,
)
from agent_core.watchdog import watchdog_manager

# Cebar lectura inicial de CPU para consultas instantáneas no bloqueantes en /api/status
psutil.cpu_percent(interval=None)

WHATSAPP_DIR = BASE_DIR / "whatsapp_bridge"
CONTACTS_CACHE_PATH = WHATSAPP_DIR / "contacts_cache.json"
_CONTACTS_LOCK = threading.Lock()
DASHBOARD_HTML_PATH = BASE_DIR / "agent_core" / "dashboard.html"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

app = FastAPI(
    title="Agente Personal UNSA & PC Controller",
    version="2.0.0",
    description="Backend local FastAPI potenciado por Google Gemini, WhatsApp y Dashboard Cockpit.",
)

agent = UniversityPCAgent()


def cleanup_old_temp_media(max_age_sec: int = 86400, temp_dir: Path | None = None) -> int:
    """Elimina en `temp/` únicamente imágenes y audios (*.png, *.jpg, *.jpeg, *.ogg) con >24h."""
    target_dir = temp_dir or TEMP_DIR
    if not target_dir.exists() or not target_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_sec
    media_exts = {".png", ".jpg", ".jpeg", ".ogg"}
    removed = 0
    for item in target_dir.iterdir():
        if not item.is_file() or item.suffix.lower() not in media_exts:
            continue
        try:
            if item.stat().st_mtime < cutoff:
                item.unlink()
                removed += 1
        except OSError:
            continue
    return removed


@app.on_event("startup")
def _on_server_startup() -> None:
    """Limpieza inicial de seguridad y mantenimiento al arrancar el servidor."""
    try:
        cleanup_old_confirmations(max_age_sec=86400)
    except Exception:
        pass
    try:
        cleanup_old_temp_media(max_age_sec=86400)
    except Exception:
        pass

_WA_STATE_LOCK = threading.Lock()
_WA_STATE: dict[str, Any] = {
    "status": "unknown",
    "detail": "Esperando conexión del puente WhatsApp",
    "qr_text": "",
    "updated_at": "",
}


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    sender: str = Field(default="default")
    source: str = Field(default="whatsapp")


class AudioRequest(BaseModel):
    audio_path: str = Field(..., min_length=1)
    sender: str = Field(default="default")
    source: str = Field(default="whatsapp")


class ResetRequest(BaseModel):
    sender: str = Field(default="default")


class ModelRequest(BaseModel):
    model: str = Field(..., min_length=1)


class MonitorSelectRequest(BaseModel):
    mode: int


class MonitorCaptureRequest(BaseModel):
    monitor_number: int | None = None


class WhatsAppStateRequest(BaseModel):
    status: str = Field(..., min_length=1)
    detail: str = Field(default="")
    qr_text: str = Field(default="")


class BrainNoteRequest(BaseModel):
    topic: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    tags: str = Field(default="universidad")


class AlertCreateRequest(BaseModel):
    condition_type: str = Field(..., min_length=1)
    target_value: str = Field(default="")
    message: str = Field(default="")


class WhatsAppContactRequest(BaseModel):
    name: str = Field(..., min_length=1)
    phone: str = Field(..., min_length=1)


class AgentResponse(BaseModel):
    reply: str
    image_path: str | None = None
    images: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    model: str | None = None
    duration_ms: int | None = None


def _find_whatsapp_bridge_procs() -> list[psutil.Process]:
    """Localiza procesos Node.js ejecutando whatsapp_bridge/index.js."""
    matches: list[psutil.Process] = []
    wa_dir_resolved = WHATSAPP_DIR.resolve()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in ("node.exe", "node"):
                continue
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            is_bridge = "whatsapp_bridge" in cmdline
            if not is_bridge and "index.js" in cmdline:
                try:
                    if Path(proc.cwd()).resolve() == wa_dir_resolved:
                        is_bridge = True
                except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                    pass
            if is_bridge:
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def _kill_whatsapp_bridge() -> int:
    """Termina limpiamente cualquier instancia del puente de WhatsApp."""
    procs = _find_whatsapp_bridge_procs()
    killed = 0
    for proc in procs:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=5,
                check=False,
            )
            killed += 1
        except Exception:
            try:
                proc.kill()
                killed += 1
            except Exception:
                pass
    return killed


def _normalize_phone_digits(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    if len(digits) == 9 and digits.startswith("9"):
        return f"51{digits}"
    return digits


def _read_contacts_cache(limit: int = 60) -> list[dict[str, Any]]:
    with _CONTACTS_LOCK:
        if not CONTACTS_CACHE_PATH.exists():
            return []
        try:
            raw = json.loads(CONTACTS_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(raw, list):
            return []

    cleaned: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("jid"):
            continue
        display = str(
            item.get("name")
            or item.get("notify")
            or item.get("verifiedName")
            or item.get("phone")
            or ""
        ).strip()
        if not display:
            continue
        cleaned.append(
            {
                "jid": str(item.get("jid")),
                "name": display,
                "phone": str(item.get("phone") or ""),
                "vip": bool(item.get("vip")),
            }
        )

    cleaned.sort(key=lambda c: (not c["vip"], c["name"].lower()))
    return cleaned[:limit]


def _sync_bridge_contact(method: str, payload: dict[str, Any] | None = None, query: str = "") -> None:
    url = f"http://127.0.0.1:8001/contacts{query}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=1.5):
            pass
    except Exception:
        # El disco ya quedó sincronizado; el bridge lo leerá al iniciar si estaba apagado.
        pass


def _upsert_vip_contact(name: str, phone: str) -> dict[str, Any]:
    clean_name = name.strip()
    digits = _normalize_phone_digits(phone)
    if not clean_name or len(digits) < 7:
        raise ValueError("Indica un nombre/alias y un teléfono válido (mínimo 7 dígitos).")

    jid = f"{digits}@s.whatsapp.net"
    entry = {
        "jid": jid,
        "phone": digits,
        "name": clean_name,
        "notify": clean_name,
        "verifiedName": "",
        "vip": True,
        "updatedAt": int(time.time() * 1000),
    }

    with _CONTACTS_LOCK:
        CONTACTS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list[dict[str, Any]] = []
        if CONTACTS_CACHE_PATH.exists():
            try:
                loaded = json.loads(CONTACTS_CACHE_PATH.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = loaded
            except Exception:
                existing = []
        filtered = [item for item in existing if isinstance(item, dict) and item.get("jid") != jid]
        filtered.insert(0, entry)
        CONTACTS_CACHE_PATH.write_text(
            json.dumps(filtered, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    _sync_bridge_contact("POST", {"name": clean_name, "phone": digits})
    return entry


def _delete_vip_contact(jid: str) -> bool:
    clean_jid = urllib.parse.unquote(jid).strip()
    if not clean_jid:
        return False

    removed = False
    with _CONTACTS_LOCK:
        if CONTACTS_CACHE_PATH.exists():
            try:
                loaded = json.loads(CONTACTS_CACHE_PATH.read_text(encoding="utf-8"))
            except Exception:
                loaded = []
            if isinstance(loaded, list):
                kept = [item for item in loaded if isinstance(item, dict) and item.get("jid") != clean_jid]
                removed = len(kept) < len(loaded)
                CONTACTS_CACHE_PATH.write_text(
                    json.dumps(kept, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

    _sync_bridge_contact("DELETE", query=f"?jid={urllib.parse.quote(clean_jid)}")
    return removed


def _all_configured_models() -> list[str]:
    ordered: list[str] = []
    for m in [*GEMINI_MODELS_FAST, *GEMINI_MODELS_COMPLEX, GEMINI_MODEL, *DISCARDED_MODELS, *DEAD_MODELS]:
        if m and m not in ordered:
            ordered.append(m)
    return ordered


@app.get("/")
def root_redirect() -> RedirectResponse:
    """Redirige la raíz al Dashboard interactivo."""
    return RedirectResponse(url="/dashboard")


@app.get("/dashboard", response_class=HTMLResponse)
def serve_dashboard() -> HTMLResponse:
    """Sirve el Dashboard Cockpit Pro para modo app en Brave."""
    if not DASHBOARD_HTML_PATH.exists():
        raise HTTPException(status_code=404, detail="dashboard.html no encontrado.")
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=html)


@app.get("/api/preview-image")
def get_preview_image(name: str) -> FileResponse:
    """Sirve de forma segura capturas generadas dentro de temp/."""
    safe_name = Path(name).name
    target = (TEMP_DIR / safe_name).resolve()
    if not target.exists() or not target.is_file() or target.parent != TEMP_DIR.resolve():
        raise HTTPException(status_code=404, detail="Imagen no encontrada.")
    return FileResponse(path=str(target), media_type="image/png")


@app.get("/api/generated-files/download")
def download_generated_artifact(rel_path: str) -> FileResponse:
    """Sirve de forma segura un artefacto compilado dentro de generated_projects/."""
    base_resolved = GENERATED_PROJECTS_DIR.resolve()
    target = (base_resolved / rel_path).resolve()
    if (
        not target.exists()
        or not target.is_file()
        or base_resolved not in target.parents
    ):
        raise HTTPException(status_code=404, detail="Archivo generado no encontrado.")
    return FileResponse(path=str(target), filename=target.name)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Verifica el estado del servidor, modelo configurado y hora local en Lima."""
    return {
        "status": "ok",
        "model": get_env("GEMINI_MODEL", GEMINI_MODEL) or GEMINI_MODEL,
        "active_model_mode": agent.forced_model,
        "lima_time": datetime.now(LIMA_TZ).isoformat(),
    }


@app.get("/api/status")
def api_status(brain_q: str = "") -> dict[str, Any]:
    """Devuelve telemetría, WhatsApp, monitores, modelos, Segundo Cerebro, alertas y timeline."""
    now_lima = datetime.now(LIMA_TZ)
    cpu_pct = float(psutil.cpu_percent(interval=None))
    vm = psutil.virtual_memory()
    battery = psutil.sensors_battery()

    if battery is not None:
        battery_info = {
            "available": True,
            "percent": round(float(battery.percent), 1),
            "plugged": bool(battery.power_plugged),
        }
    else:
        battery_info = {
            "available": False,
            "percent": None,
            "plugged": True,
        }

    wa_procs = _find_whatsapp_bridge_procs()
    with _WA_STATE_LOCK:
        wa_snapshot = dict(_WA_STATE)

    if not wa_procs:
        effective_wa_status = "stopped"
        wa_detail = "Puente de WhatsApp detenido"
    else:
        effective_wa_status = wa_snapshot["status"]
        wa_detail = wa_snapshot["detail"]
        if effective_wa_status in ("unknown", "disconnected"):
            try:
                uptime_sec = time.time() - wa_procs[0].create_time()
            except Exception:
                uptime_sec = 99
            effective_wa_status = "connecting" if uptime_sec < 10 else "connected"
            wa_detail = (
                "Sincronizando sesión..." if effective_wa_status == "connecting" else "Conectado"
            )

    if effective_wa_status == "connected":
        global_status = "active"
    elif effective_wa_status in ("syncing", "connecting", "qr"):
        global_status = "syncing"
    else:
        global_status = "stopped"

    monitors = _get_monitors_dynamic()
    default_mode = get_default_monitor_mode()
    if monitors and default_mode > len(monitors):
        default_mode = set_default_monitor_mode(0)
    cooldowns = agent.get_active_cooldowns()
    val_status = get_model_validation_status()
    discarded: list[str] = []
    for dm in [*val_status.get("all_discarded", []), *DISCARDED_MODELS, *DEAD_MODELS]:
        if dm and dm not in discarded:
            discarded.append(dm)
    all_models = _all_configured_models()
    alive_models = [m for m in all_models if m not in discarded]

    return {
        "global_status": global_status,
        "lima": {
            "time": now_lima.strftime("%H:%M:%S"),
            "date": now_lima.strftime("%Y-%m-%d"),
            "timezone": "UTC-5 (America/Lima)",
        },
        "whatsapp": {
            "status": effective_wa_status,
            "detail": wa_detail,
            "pid": wa_procs[0].pid if wa_procs else None,
            "qr_text": wa_snapshot.get("qr_text", "") if effective_wa_status == "qr" else "",
        },
        "telemetry": {
            "cpu_percent": round(cpu_pct, 1),
            "ram_percent": round(float(vm.percent), 1),
            "ram_used_gb": round(vm.used / (1024**3), 2),
            "ram_total_gb": round(vm.total / (1024**3), 2),
            "battery": battery_info,
        },
        "models": {
            "forced_mode": agent.forced_model,
            "last_used_model": agent.last_used_model,
            "all_models": all_models,
            "alive_models": alive_models,
            "discarded_models": discarded,
            "fast_queue": list(GEMINI_MODELS_FAST),
            "complex_queue": list(GEMINI_MODELS_COMPLEX),
            "cooldowns": cooldowns,
            "capabilities": {k: dict(v) for k, v in MODEL_CAPABILITIES.items()},
            "validation": val_status,
        },
        "monitors": {
            "default_mode": default_mode,
            "detected": monitors,
        },
        "workers": agent.get_workers_status(),
        "generated_projects": list_generated_projects(limit=25),
        "second_brain": search_notes(brain_q, limit=30),
        "alerts": watchdog_manager.list_alerts(limit=25),
        "whatsapp_contacts": _read_contacts_cache(limit=60),
        "activity": agent.get_activity_log(40),
    }


@app.get("/api/whatsapp/contacts")
@app.get("/api/contacts")
def list_whatsapp_contacts_endpoint() -> dict[str, Any]:
    """Lista contactos guardados y VIP disponibles para envíos rápidos por WhatsApp."""
    contacts = _read_contacts_cache(limit=100)
    return {"status": "ok", "count": len(contacts), "contacts": contacts}


@app.post("/api/whatsapp/contacts")
def save_whatsapp_contact_endpoint(payload: WhatsAppContactRequest) -> dict[str, Any]:
    """Guarda o actualiza un contacto VIP en contacts_cache.json y en el puente Node."""
    try:
        entry = _upsert_vip_contact(name=payload.name, phone=payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    agent.log_event(
        "whatsapp",
        f"Contacto VIP guardado: {entry['name']}",
        f"Teléfono: +{entry['phone']}",
    )
    return {"status": "ok", "contact": entry}


@app.delete("/api/whatsapp/contacts/{jid:path}")
def delete_whatsapp_contact_endpoint(jid: str) -> dict[str, Any]:
    """Elimina un contacto guardado de contacts_cache.json y del puente WhatsApp."""
    removed = _delete_vip_contact(jid)
    agent.log_event("whatsapp", "Contacto VIP eliminado", jid, status="warn" if removed else "ok")
    return {"status": "ok", "deleted": removed, "jid": jid}


@app.get("/api/brain")
def list_brain_notes_endpoint(q: str = "", limit: int = 30) -> dict[str, Any]:
    """Lista o busca notas permanentes en el Segundo Cerebro SQLite."""
    notes = search_notes(q, limit=limit)
    return {"status": "ok", "count": len(notes), "notes": notes}


@app.post("/api/brain/save")
def save_brain_note_endpoint(payload: BrainNoteRequest) -> dict[str, Any]:
    """Guarda una nota permanente en SQLite desde el Dashboard."""
    try:
        note = save_note(topic=payload.topic, content=payload.content, tags=payload.tags)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    agent.log_event("memory", "Nota guardada (Segundo Cerebro)", note["topic"])
    return {"status": "ok", "note": note}


@app.delete("/api/brain/{note_id}")
def delete_brain_note_endpoint(note_id: int) -> dict[str, Any]:
    """Elimina una nota del Segundo Cerebro por ID."""
    deleted = delete_note(note_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Nota no encontrada.")
    agent.log_event("memory", f"Nota #{note_id} eliminada", "Actualizado desde Dashboard")
    return {"status": "ok", "deleted": True}


@app.get("/api/alerts")
def list_alerts_endpoint(limit: int = 25) -> dict[str, Any]:
    """Lista las alertas activas y recientes del Vigilante Proactivo."""
    alerts = watchdog_manager.list_alerts(limit=limit)
    return {"status": "ok", "count": len(alerts), "alerts": alerts}


@app.post("/api/alerts/create")
def create_alert_endpoint(payload: AlertCreateRequest) -> dict[str, Any]:
    """Crea un vigilante proactivo hacia WhatsApp desde el Dashboard."""
    try:
        alert = watchdog_manager.add_alert(
            condition_type=payload.condition_type,
            target_value=payload.target_value,
            message=payload.message,
            dutic_fn=get_dutic_events,
            screenshot_fn=take_screenshot,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    agent.log_event("alert", f"Alerta #{alert['id']} activada", alert["detail"])
    return {"status": "ok", "alert": alert}


@app.delete("/api/alerts/{alert_id}")
def cancel_alert_endpoint(alert_id: int) -> dict[str, Any]:
    """Cancela una alerta activa del vigilante proactivo."""
    cancelled = watchdog_manager.cancel_alert(alert_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Alerta no encontrada o ya finalizada.")
    agent.log_event("alert", f"Alerta #{alert_id} cancelada", "Cancelada desde Dashboard", status="warn")
    return {"status": "ok", "cancelled": True}


@app.post("/model")
def set_model_endpoint(payload: ModelRequest) -> dict[str, str]:
    """Cambia en caliente el modelo preferido (o 'Auto') desde el Dashboard."""
    active = agent.set_forced_model(payload.model)
    return {"status": "ok", "active_model_mode": active}


@app.get("/api/monitors")
def list_monitors_endpoint() -> dict[str, Any]:
    """Devuelve las dimensiones físicas dinámicas (Per-Monitor DPI Aware) de los monitores conectados."""
    monitors = _get_monitors_dynamic()
    return {
        "status": "ok",
        "count": len(monitors),
        "default_mode": get_default_monitor_mode(),
        "monitors": monitors,
    }


@app.post("/api/monitor/select")
def select_monitor_endpoint(payload: MonitorSelectRequest) -> dict[str, Any]:
    """Configura dinámicamente el monitor por defecto entre los actualmente conectados."""
    monitors = _get_monitors_dynamic()
    max_connected = max(1, len(monitors))
    if payload.mode not in (-1, 0, *range(1, max_connected + 1)):
        raise HTTPException(
            status_code=400,
            detail=f"Modo de monitor inválido o desconectado (monitores conectados: {max_connected}).",
        )
    mode = set_default_monitor_mode(payload.mode)
    labels: dict[int, str] = {
        0: "Auto (Monitor de ventana activa)",
        -1: "Panorámica (Todos los monitores)",
    }
    for m in monitors:
        labels[int(m["id"])] = str(m.get("name") or f"Monitor {m['id']}")
    agent.log_event("monitor", "Monitor predeterminado actualizado", labels.get(mode, f"Modo {mode}"))
    return {"status": "ok", "default_mode": mode, "label": labels.get(mode, f"Monitor {mode}")}


@app.post("/api/monitor/capture")
def capture_monitor_preview(payload: MonitorCaptureRequest) -> dict[str, Any]:
    """Toma una captura del monitor elegido y la devuelve lista para previsualización en Dashboard."""
    target = (
        int(payload.monitor_number)
        if payload.monitor_number is not None
        else get_default_monitor_mode()
    )
    res = capture_monitor_image(monitor_number=target, enqueue=False)
    if not res.get("ok"):
        return {
            "ok": False,
            "error": res.get("error", "No se pudo capturar la pantalla."),
        }

    img_path = Path(str(res["path"]))
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    agent.log_event(
        "monitor",
        f"Previsualización {res['label']}",
        f"{res['width']}x{res['height']}",
    )
    return {
        "ok": True,
        "label": res["label"],
        "monitor_index": res["monitor_index"],
        "total_monitors": res["total_monitors"],
        "width": res["width"],
        "height": res["height"],
        "image_name": img_path.name,
        "image_url": f"/api/preview-image?name={img_path.name}",
        "image_data_url": f"data:image/png;base64,{b64}",
    }


@app.post("/api/whatsapp/state")
def update_whatsapp_state(payload: WhatsAppStateRequest) -> dict[str, str]:
    """Recibe actualizaciones de estado en tiempo real desde whatsapp_bridge/index.js."""
    prev_status = ""
    with _WA_STATE_LOCK:
        prev_status = str(_WA_STATE.get("status", ""))
        _WA_STATE["status"] = payload.status.strip()
        _WA_STATE["detail"] = payload.detail.strip()
        _WA_STATE["qr_text"] = payload.qr_text or ""
        _WA_STATE["updated_at"] = datetime.now(LIMA_TZ).isoformat()

    if payload.status != prev_status:
        title_map = {
            "syncing": "WhatsApp sincronizando (escudo 8s)",
            "connected": "WhatsApp conectado y listo",
            "qr": "WhatsApp requiere escanear QR",
            "disconnected": "WhatsApp desconectado",
        }
        status_flag = "warn" if payload.status in ("disconnected", "qr") else "ok"
        agent.log_event(
            "whatsapp",
            title_map.get(payload.status, f"WhatsApp: {payload.status}"),
            payload.detail,
            status=status_flag,
        )
    return {"status": "ok"}


@app.post("/api/whatsapp/restart")
def restart_whatsapp_bridge() -> dict[str, Any]:
    """Reinicia limpiamente el proceso Node.js de whatsapp_bridge."""
    killed = _kill_whatsapp_bridge()
    time.sleep(0.4)
    if not WHATSAPP_DIR.exists():
        raise HTTPException(status_code=500, detail="Carpeta whatsapp_bridge no encontrada.")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    wa_log = open(TEMP_DIR / "whatsapp.log", "a", encoding="utf-8", errors="replace")
    try:
        proc = subprocess.Popen(
            ["node", "index.js"],
            cwd=str(WHATSAPP_DIR),
            env=env,
            stdout=wa_log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
        )
    finally:
        wa_log.close()
    with _WA_STATE_LOCK:
        _WA_STATE["status"] = "connecting"
        _WA_STATE["detail"] = f"Reiniciando puente (PID {proc.pid})..."
        _WA_STATE["qr_text"] = ""
        _WA_STATE["updated_at"] = datetime.now(LIMA_TZ).isoformat()

    agent.log_event(
        "whatsapp",
        "Puente de WhatsApp reiniciado",
        f"Procesos previos cerrados: {killed} | Nuevo PID: {proc.pid}",
    )
    return {"status": "ok", "killed": killed, "pid": proc.pid}


@app.post("/system/shutdown")
def system_shutdown() -> dict[str, str]:
    """Detiene limpiamente WhatsApp Bridge y apaga el servidor FastAPI."""
    agent.log_event("system", "Apagado total solicitado", "Cerrando puente y servidor", status="warn")
    _kill_whatsapp_bridge()

    def _delayed_exit() -> None:
        time.sleep(0.35)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"status": "shutting_down", "message": "Sistema apagándose limpiamente."}


@app.post("/chat", response_model=AgentResponse)
def chat_endpoint(payload: ChatRequest) -> AgentResponse | JSONResponse:
    """Procesa un mensaje de texto enviado desde WhatsApp o Dashboard PC."""
    try:
        result = agent.process_text(
            message=payload.message,
            sender=payload.sender,
            source=payload.source,
        )
        return AgentResponse(**result)
    except Exception as exc:
        traceback.print_exc()
        agent.log_event("error", "Error controlado en POST /chat", str(exc), status="error")
        return JSONResponse(
            status_code=200,
            content={
                "reply": f"⚠️ Error interno controlado: {str(exc)}",
                "image_path": None,
                "images": [],
                "files": [],
                "model": "error",
                "duration_ms": 0,
            },
        )


@app.post("/audio", response_model=AgentResponse)
def audio_endpoint(payload: AudioRequest) -> AgentResponse | JSONResponse:
    """Procesa una nota de voz (.ogg) y limpia el archivo temporal tras procesarlo."""
    temp_root = TEMP_DIR.resolve()
    try:
        audio_file = Path(payload.audio_path).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Ruta de audio inválida.")

    if not audio_file.is_relative_to(temp_root) or audio_file == temp_root:
        raise HTTPException(
            status_code=403,
            detail="Ruta de audio no permitida: debe ubicarse dentro de temp/.",
        )

    if not audio_file.is_file():
        return JSONResponse(
            status_code=200,
            content={
                "reply": f"⚠️ Archivo de audio no encontrado: {audio_file.name}",
                "image_path": None,
                "images": [],
                "files": [],
                "model": "error",
                "duration_ms": 0,
            },
        )

    try:
        result = agent.process_audio(
            audio_path=str(audio_file),
            sender=payload.sender,
            source=payload.source,
        )
        return AgentResponse(**result)
    except Exception as exc:
        traceback.print_exc()
        agent.log_event("error", "Error controlado en POST /audio", str(exc), status="error")
        return JSONResponse(
            status_code=200,
            content={
                "reply": f"⚠️ Error interno controlado: {str(exc)}",
                "image_path": None,
                "images": [],
                "files": [],
                "model": "error",
                "duration_ms": 0,
            },
        )
    finally:
        try:
            if audio_file.is_relative_to(temp_root) and audio_file.is_file():
                audio_file.unlink()
        except OSError:
            pass


@app.post("/reset")
def reset_endpoint(payload: ResetRequest) -> dict[str, str]:
    """Reinicia la memoria reciente de conversación para el remitente indicado."""
    agent.reset_session(sender=payload.sender)
    return {"status": "ok", "message": f"Memoria limpiada para '{payload.sender}'."}


if __name__ == "__main__":
    import uvicorn

    host = get_env("AGENT_HOST", "127.0.0.1") or "127.0.0.1"
    port = int(get_env("AGENT_PORT", "8000") or "8000")
    uvicorn.run("agent_core.server:app", host=host, port=port, reload=False)
