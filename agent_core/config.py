"""Configuración centralizada y carga de variables de entorno."""

from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR: Path = Path(__file__).resolve().parent.parent
ENV_PATH: Path = BASE_DIR / ".env"

# Cargar variables desde .env en la raíz del proyecto
load_dotenv(dotenv_path=ENV_PATH, override=True)

TEMP_DIR: Path = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
BACKEND_LOG_PATH: Path = TEMP_DIR / "backend.log"
_BACKEND_LOG_LOCK: threading.Lock = threading.Lock()

CREDENTIALS_PATH: Path = BASE_DIR / "credentials.json"
TOKEN_PATH: Path = BASE_DIR / "token.json"

LIMA_TZ: ZoneInfo = ZoneInfo("America/Lima")

TASK_MAX_DURATION_SEC: int = 360
HEAVY_TASK_MAX_DURATION_SEC: int = 620

CONFIRM_CRITICAL_ACTIONS: bool = (
    os.getenv("CONFIRM_CRITICAL_ACTIONS", "true").strip().lower()
    in ("1", "true", "yes", "si", "sí")
)
SEND_SCREENSHOTS_TO_MODEL: bool = (
    os.getenv("SEND_SCREENSHOTS_TO_MODEL", "true").strip().lower()
    in ("1", "true", "yes", "si", "sí")
)
USE_OFFICIAL_COMPUTER_USE: bool = (
    os.getenv("USE_OFFICIAL_COMPUTER_USE", "false").strip().lower()
    in ("1", "true", "yes", "si", "sí")
)

GOOGLE_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/calendar",
]

def get_env(name: str, default: str = "") -> str:
    """Obtiene una variable de entorno recargando .env si es necesario."""
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    return os.getenv(name, default).strip()


def _parse_model_list(raw: str) -> list[str]:
    """Convierte una lista separada por comas en una lista única ordenada de modelos."""
    models: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in models:
            models.append(cleaned)
    return models


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_MODELS_FAST = (
    "gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.1-flash-lite-preview,"
    "gemini-3-flash-preview,gemini-4-hydrogen,gemini-3.8-flash,gemini-3.6-flash"
)
DEFAULT_MODELS_COMPLEX = (
    "gemini-4-hydrogen,gemini-3-flash-preview,gemini-3.5-flash-lite,"
    "gemini-flash-lite-latest,gemini-3.1-flash-lite-preview,gemini-3.8-flash,gemini-3.6-flash"
)

GEMINI_MODEL: str = get_env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL
GEMINI_MODELS_FAST: list[str] = _parse_model_list(
    get_env("GEMINI_MODELS_FAST", DEFAULT_MODELS_FAST) or DEFAULT_MODELS_FAST
)
GEMINI_MODELS_COMPLEX: list[str] = _parse_model_list(
    get_env("GEMINI_MODELS_COMPLEX", DEFAULT_MODELS_COMPLEX) or DEFAULT_MODELS_COMPLEX
)

COOLDOWN_RPM_SECONDS: int = 65
COOLDOWN_DAILY_SECONDS: int = 3600
DEAD_MODELS: set[str] = set()
DISCARDED_MODELS: list[str] = []
MODEL_COOLDOWNS: dict[str, float] = {}
MODEL_STATE_LOCK: threading.Lock = threading.Lock()

# Capacidades multimodales validadas empíricamente en v1beta (Fase 0.2):
# - "same_content": FunctionResponse + Part.from_bytes(image/jpeg) en el mismo Content(role="user")
# - "separate_user_content": FunctionResponse primero, luego Content(role="user", parts=[texto, image])
MODEL_CAPABILITIES: dict[str, dict[str, Any]] = {
    "gemini-3.5-flash-lite": {
        "multimodal_tool_response": "same_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
    "gemini-flash-lite-latest": {
        "multimodal_tool_response": "same_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
    "gemini-3.1-flash-lite-preview": {
        "multimodal_tool_response": "same_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
    "gemini-3-flash-preview": {
        "multimodal_tool_response": "same_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
    "gemini-4-hydrogen": {
        "multimodal_tool_response": "same_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
    "gemini-3.6-flash": {
        "multimodal_tool_response": "same_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
    "gemini-3.8-flash": {
        "multimodal_tool_response": "separate_user_content",
        "fallback": "separate_user_content",
        "verified": True,
    },
}

MODEL_VALIDATION_STATUS: dict[str, Any] = {
    "checked": False,
    "checked_at": "",
    "fast_alive": list(GEMINI_MODELS_FAST),
    "fast_discarded": [],
    "complex_alive": list(GEMINI_MODELS_COMPLEX),
    "complex_discarded": [],
    "all_alive": [],
    "all_discarded": [],
    "total_api_gemini_models": 0,
    "error": "",
}


def append_backend_log(line: str) -> None:
    """Escribe una línea con timestamp America/Lima en temp/backend.log de forma thread-safe."""
    stamp = datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{stamp}] {line.rstrip()}\n"
    with _BACKEND_LOG_LOCK:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        with BACKEND_LOG_PATH.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(entry)


def get_model_multimodal_mode(model_name: str) -> str:
    """Devuelve el modo multimodal validado ('same_content' o 'separate_user_content')."""
    with MODEL_STATE_LOCK:
        cap = MODEL_CAPABILITIES.get(model_name, {})
        mode = str(cap.get("multimodal_tool_response") or "separate_user_content")
        return mode if mode in ("same_content", "separate_user_content") else "separate_user_content"


def set_model_multimodal_mode(model_name: str, mode: str) -> None:
    """Actualiza dinámicamente el modo multimodal de un modelo (ej. al detectar 400 fallback)."""
    normalized = mode if mode in ("same_content", "separate_user_content") else "separate_user_content"
    with MODEL_STATE_LOCK:
        entry = MODEL_CAPABILITIES.setdefault(
            model_name,
            {
                "multimodal_tool_response": normalized,
                "fallback": "separate_user_content",
                "verified": True,
            },
        )
        entry["multimodal_tool_response"] = normalized
        entry["verified"] = True


def get_model_validation_status() -> dict[str, Any]:
    """Devuelve una copia del estado de validación inicial de modelos."""
    with MODEL_STATE_LOCK:
        return {
            "checked": bool(MODEL_VALIDATION_STATUS.get("checked")),
            "checked_at": str(MODEL_VALIDATION_STATUS.get("checked_at") or ""),
            "fast_alive": list(MODEL_VALIDATION_STATUS.get("fast_alive") or []),
            "fast_discarded": list(MODEL_VALIDATION_STATUS.get("fast_discarded") or []),
            "complex_alive": list(MODEL_VALIDATION_STATUS.get("complex_alive") or []),
            "complex_discarded": list(MODEL_VALIDATION_STATUS.get("complex_discarded") or []),
            "all_alive": list(MODEL_VALIDATION_STATUS.get("all_alive") or []),
            "all_discarded": list(MODEL_VALIDATION_STATUS.get("all_discarded") or []),
            "total_api_gemini_models": int(MODEL_VALIDATION_STATUS.get("total_api_gemini_models") or 0),
            "error": str(MODEL_VALIDATION_STATUS.get("error") or ""),
        }


def validate_models_at_startup(api_key: str | None = None) -> dict[str, Any]:
    """Consulta client.models.list() y descarta modelos 404 antes de la primera petición."""
    global GEMINI_MODEL

    key = (api_key or get_env("GEMINI_API_KEY")).strip()
    checked_at = datetime.now(LIMA_TZ).isoformat()
    if not key or key == "TU_GEMINI_API_KEY_AQUI":
        with MODEL_STATE_LOCK:
            MODEL_VALIDATION_STATUS.update(
                {
                    "checked": False,
                    "checked_at": checked_at,
                    "fast_alive": list(GEMINI_MODELS_FAST),
                    "fast_discarded": [],
                    "complex_alive": list(GEMINI_MODELS_COMPLEX),
                    "complex_discarded": [],
                    "all_alive": [],
                    "all_discarded": [],
                    "total_api_gemini_models": 0,
                    "error": "GEMINI_API_KEY no configurada.",
                }
            )
        return get_model_validation_status()

    try:
        from google import genai

        client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})
        available_set: set[str] = set()
        gemini_count = 0
        for item in client.models.list():
            raw_name = getattr(item, "name", "") or ""
            short_name = raw_name.removeprefix("models/")
            if raw_name:
                available_set.add(raw_name)
            if short_name:
                available_set.add(short_name)
                if "gemini" in short_name.lower():
                    gemini_count += 1
    except Exception as exc:
        err_msg = str(exc)
        with MODEL_STATE_LOCK:
            MODEL_VALIDATION_STATUS.update(
                {
                    "checked": False,
                    "checked_at": checked_at,
                    "fast_alive": list(GEMINI_MODELS_FAST),
                    "fast_discarded": [],
                    "complex_alive": list(GEMINI_MODELS_COMPLEX),
                    "complex_discarded": [],
                    "all_alive": [],
                    "all_discarded": [],
                    "total_api_gemini_models": 0,
                    "error": err_msg,
                }
            )
        append_backend_log(f"[ModelsInit] Error al consultar client.models.list(): {err_msg}")
        return get_model_validation_status()

    with MODEL_STATE_LOCK:
        fast_alive = [m for m in GEMINI_MODELS_FAST if m in available_set]
        fast_discarded = [m for m in GEMINI_MODELS_FAST if m not in available_set]
        complex_alive = [m for m in GEMINI_MODELS_COMPLEX if m in available_set]
        complex_discarded = [m for m in GEMINI_MODELS_COMPLEX if m not in available_set]

        GEMINI_MODELS_FAST[:] = fast_alive
        GEMINI_MODELS_COMPLEX[:] = complex_alive

        all_discarded: list[str] = []
        for m in [*fast_discarded, *complex_discarded]:
            if m not in all_discarded:
                all_discarded.append(m)
            DEAD_MODELS.add(m)
            MODEL_COOLDOWNS.pop(m, None)

        if GEMINI_MODEL and GEMINI_MODEL not in available_set:
            if GEMINI_MODEL not in all_discarded:
                all_discarded.append(GEMINI_MODEL)
            DEAD_MODELS.add(GEMINI_MODEL)
            MODEL_COOLDOWNS.pop(GEMINI_MODEL, None)
            if fast_alive:
                GEMINI_MODEL = fast_alive[0]
            elif complex_alive:
                GEMINI_MODEL = complex_alive[0]

        all_alive: list[str] = []
        for m in [*fast_alive, *complex_alive, GEMINI_MODEL]:
            if m and m in available_set and m not in all_alive:
                all_alive.append(m)

        DISCARDED_MODELS[:] = all_discarded
        MODEL_VALIDATION_STATUS.update(
            {
                "checked": True,
                "checked_at": checked_at,
                "fast_alive": fast_alive,
                "fast_discarded": fast_discarded,
                "complex_alive": complex_alive,
                "complex_discarded": complex_discarded,
                "all_alive": all_alive,
                "all_discarded": all_discarded,
                "total_api_gemini_models": gemini_count,
                "error": "",
            }
        )

    append_backend_log(
        f"[ModelsInit] Vivos FAST={fast_alive} | Vivos COMPLEX={complex_alive} | Descartados(404)={all_discarded}"
    )
    return get_model_validation_status()

_RETRY_DELAY_RE = re.compile(
    r"(?:retryDelay['\"]?\s*:\s*['\"]?|retry\s+in\s+)(\d+(?:\.\d+)?)\s*s",
    re.IGNORECASE,
)


def _extract_retry_delay_seconds(err_str: str) -> float:
    """Extrae el valor de retryDelay en segundos de un error 429 si está presente."""
    match = _RETRY_DELAY_RE.search(err_str or "")
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except (TypeError, ValueError):
        return 0.0


def classify_and_record_model_error(model_name: str, err_str: str) -> tuple[str, int]:
    """Clasifica errores 404/429 de Gemini y registra DEAD_MODELS o cooldown inteligente."""
    lower = (err_str or "").lower()
    is_404 = (
        "404" in lower
        or "not_found" in lower
        or "no longer available" in lower
    )
    if is_404:
        with MODEL_STATE_LOCK:
            DEAD_MODELS.add(model_name)
            if model_name not in DISCARDED_MODELS:
                DISCARDED_MODELS.append(model_name)
            MODEL_COOLDOWNS.pop(model_name, None)
            all_disc = list(MODEL_VALIDATION_STATUS.get("all_discarded") or [])
            if model_name not in all_disc:
                all_disc.append(model_name)
            MODEL_VALIDATION_STATUS["all_discarded"] = all_disc
            MODEL_VALIDATION_STATUS["all_alive"] = [
                m for m in (MODEL_VALIDATION_STATUS.get("all_alive") or []) if m != model_name
            ]
        return "dead_404", 0

    is_429 = any(
        token in lower
        for token in ("429", "resource_exhausted", "quota", "rate limit", "too many requests")
    )
    if is_429:
        retry_delay = _extract_retry_delay_seconds(err_str)
        is_daily = (
            "perday" in lower
            or "per day" in lower
            or "per_day" in lower
            or "limit: 0" in lower
            or retry_delay > 300.0
        )
        duration = COOLDOWN_DAILY_SECONDS if is_daily else COOLDOWN_RPM_SECONDS
        with MODEL_STATE_LOCK:
            MODEL_COOLDOWNS[model_name] = time.time() + duration
        return ("cooldown_daily" if is_daily else "cooldown_rpm"), duration

    return "other", 0


def get_active_model_cooldowns() -> dict[str, int]:
    """Devuelve segundos restantes de cooldown activo limpiando los expirados."""
    now = time.time()
    with MODEL_STATE_LOCK:
        expired = [m for m, until in MODEL_COOLDOWNS.items() if until <= now]
        for m in expired:
            MODEL_COOLDOWNS.pop(m, None)
        return {
            m: max(1, int(round(until - now)))
            for m, until in MODEL_COOLDOWNS.items()
        }


def clear_model_cooldown(model_name: str) -> None:
    """Limpia el cooldown de un modelo tras responder correctamente."""
    with MODEL_STATE_LOCK:
        MODEL_COOLDOWNS.pop(model_name, None)


def filter_available_models(
    candidates: list[str],
    respect_forced: str | None = None,
) -> list[str]:
    """Filtra DEAD_MODELS y salta en 0 ms modelos en cooldown activo."""
    now = time.time()
    with MODEL_STATE_LOCK:
        expired = [m for m, until in MODEL_COOLDOWNS.items() if until <= now]
        for m in expired:
            MODEL_COOLDOWNS.pop(m, None)
        dead_snapshot = set(DEAD_MODELS)
        cooldown_snapshot = dict(MODEL_COOLDOWNS)

    alive = [m for m in candidates if m and m not in dead_snapshot]
    if not alive:
        return []

    healthy = [m for m in alive if cooldown_snapshot.get(m, 0.0) <= now]
    if respect_forced and respect_forced != "Auto" and respect_forced in healthy:
        healthy = [respect_forced, *[m for m in healthy if m != respect_forced]]
    if healthy:
        return healthy

    # Último recurso solo si TODOS los vivos están en cooldown RPM (<= 300s), nunca los diarios (> 300s)
    short_cooling = [
        m for m in alive if 0.0 < (cooldown_snapshot.get(m, 0.0) - now) <= 300.0
    ]
    if not short_cooling:
        return []
    short_cooling.sort(key=lambda m: cooldown_snapshot.get(m, 0.0))
    return [short_cooling[0]]
