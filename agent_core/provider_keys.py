"""Gestión segura de claves hecha para Vortex AI Desktop."""

import base64
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Literal

import requests
import win32crypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from dotenv import set_key

from agent_core.admin import require_local
from agent_core.config import BASE_DIR, ENV_PATH, get_env

PATH = BASE_DIR / ".provider_keys.json"
LOCK = threading.RLock()
router = APIRouter(prefix="/api/keys", dependencies=[Depends(require_local)])
GLM_BASE = "https://api.z.ai/api/paas/v4"
GLM_FREE_MODELS = ("glm-4.7-flash", "glm-4.5-flash")


def encrypt(value):
    return base64.b64encode(win32crypt.CryptProtectData(value.encode(), "Vortex API", None, None, None, 0)).decode()


def decrypt(value):
    return win32crypt.CryptUnprotectData(base64.b64decode(value), None, None, None, 0)[1].decode()


def write_store(rows):
    pending = PATH.with_suffix(".json.pending")
    pending.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(pending, PATH)


def read_store():
    with LOCK:
        if PATH.exists():
            return json.loads(PATH.read_text(encoding="utf-8"))
        try:
            legacy = json.loads(get_env("GEMINI_API_KEYS", "[]"))
        except ValueError:
            legacy = []
        legacy = legacy or [get_env("GEMINI_API_KEY")]
        rows = []
        for index, key in enumerate(dict.fromkeys(legacy)):
            if key and key != "TU_GEMINI_API_KEY_AQUI":
                rows.append({"id": uuid.uuid4().hex, "provider": "gemini", "label": f"Gemini {index + 1}", "secret": encrypt(key), "suffix": key[-4:], "status": "untested", "models": [], "checked_at": None, "message": "Pendiente de comprobar"})
        write_store(rows)
        pending = ENV_PATH.with_suffix(".pending")
        pending.write_text(ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else "", encoding="utf-8")
        for name in ("GEMINI_API_KEY", "GEMINI_API_KEYS"):
            set_key(str(pending), name, "")
            os.environ[name] = ""
        os.replace(pending, ENV_PATH)
        return rows


def credentials(provider):
    with LOCK:
        return [(row["id"], decrypt(row["secret"]), row.get("models", [])) for row in read_store() if row["provider"] == provider]


def classify(model):
    name = model.lower()
    if any(part in name for part in ("pro", "hydrogen", "thinking", "glm-5", "glm-4.7")):
        return "complex"
    return "fast"


def text_model(name):
    if name.startswith("glm/") and name.removeprefix("glm/") not in GLM_FREE_MODELS:
        return False
    return not any(word in name.lower() for word in ("image", "imagen", "banana", "veo", "video", "tts", "embedding", "live", "transcribe", "robotics", "computer-use", "deep-research", "antigravity", "decision", "gemma", "omni", "-4v", "4.6v"))


def discover(provider, key):
    if provider == "gemini":
        result = requests.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": key}, timeout=20)
        result.raise_for_status()
        data = result.json()
        models = list(data.get("models", []))
        while data.get("nextPageToken"):
            result = requests.get("https://generativelanguage.googleapis.com/v1beta/models", headers={"x-goog-api-key": key}, params={"pageToken": data["nextPageToken"]}, timeout=20)
            result.raise_for_status()
            data = result.json()
            models.extend(data.get("models", []))
        return sorted({m["name"].removeprefix("models/") for m in models if "generateContent" in m.get("supportedGenerationMethods", []) and text_model(m["name"])})
    # Lista explícita de modelos de texto gratuitos según la tarifa oficial.
    # /models también devuelve modelos de pago y puede omitir los gratuitos.
    return list(GLM_FREE_MODELS)


def preferred_model(provider, models):
    preferred = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"] if provider == "gemini" else ["glm-4.7-flash", "glm-4.5-flash"]
    return next((m for m in preferred if m in models), next((m for m in models if "flash" in m), models[0] if models else None))


def apply_catalog():
    """Actualiza las colas existentes sin reiniciar las conversaciones."""
    from agent_core import config
    with LOCK:
        rows = read_store()
    models = list(dict.fromkeys(("glm/" if row["provider"] == "glm" else "") + model for row in rows if not (row.get("status") == "limited" and "saldo" in row.get("message", "")) for model in row.get("models", [])))
    if not models:
        if not rows:
            config.GEMINI_MODELS_FAST[:] = []
            config.GEMINI_MODELS_COMPLEX[:] = []
        return
    models = [m for m in models if text_model(m)]
    if not models:
        config.GEMINI_MODELS_FAST[:] = []
        config.GEMINI_MODELS_COMPLEX[:] = []
        return
    fast = [m for m in models if classify(m) == "fast"]
    complex_models = [m for m in models if classify(m) == "complex"]
    # Prioriza las variantes Flash en el enrutamiento automático.
    preferred = preferred_model("gemini", [m for m in fast if not m.startswith("glm/")])
    fast.sort(key=lambda m: (m != preferred, "lite" not in m, "flash" not in m, m))
    complex_models.sort(key=lambda m: ("flash" not in m, m))
    with config.MODEL_STATE_LOCK:
        config.GEMINI_MODELS_FAST[:] = fast or complex_models
        config.GEMINI_MODELS_COMPLEX[:] = complex_models or fast
        config.DEAD_MODELS.difference_update(models)
        config.DISCARDED_MODELS[:] = [m for m in config.DISCARDED_MODELS if m not in models]
    import sys
    primary = preferred_model("gemini", [m for m in models if not m.startswith("glm/")]) or (complex_models or fast)[0]
    for name in ("agent_core.config", "agent_core.agent", "agent_core.tools", "agent_core.tools_drive", "agent_core.server"):
        module = sys.modules.get(name)
        if module:
            module.GEMINI_MODEL = primary
    server = sys.modules.get("agent_core.server")
    if server and hasattr(server, "agent") and server.agent.forced_model not in ["Auto", *models]:
        server.agent.forced_model = "Auto"


class KeyInput(BaseModel):
    provider: Literal["gemini", "glm"]
    label: str = Field(min_length=1, max_length=80)
    secret: str = Field(default="", max_length=2000)


@router.get("")
def list_keys():
    with LOCK:
        return [{k: v for k, v in row.items() if k != "secret"} for row in read_store()]


@router.post("")
def add_key(payload: KeyInput):
    if not payload.secret.strip() or any(c.isspace() for c in payload.secret.strip()):
        raise HTTPException(422, "Ingresa una clave válida")
    with LOCK:
        rows = read_store()
        if any(row["provider"] == payload.provider and decrypt(row["secret"]) == payload.secret.strip() for row in rows):
            raise HTTPException(409, "Esta clave ya está guardada")
        row = {"id": uuid.uuid4().hex, "provider": payload.provider, "label": payload.label.strip(), "secret": encrypt(payload.secret.strip()), "suffix": payload.secret.strip()[-4:], "status": "untested", "models": [], "checked_at": None, "message": "Pendiente de comprobar"}
        rows.append(row)
        write_store(rows)
    return {"id": row["id"]}


@router.put("/{key_id}")
def edit_key(key_id: str, payload: KeyInput):
    with LOCK:
        rows = read_store()
        row = next((row for row in rows if row["id"] == key_id), None)
        if row is None:
            raise HTTPException(404, "Clave no encontrada")
        if payload.secret.strip():
            if any(c.isspace() for c in payload.secret.strip()):
                raise HTTPException(422, "La clave debe ocupar una sola línea")
            if any(item["id"] != key_id and item["provider"] == payload.provider and decrypt(item["secret"]) == payload.secret.strip() for item in rows):
                raise HTTPException(409, "Esta clave ya está guardada")
            row.update(secret=encrypt(payload.secret.strip()), suffix=payload.secret.strip()[-4:], models=[], status="untested", checked_at=None, message="Pendiente de comprobar")
        if row["provider"] != payload.provider:
            row.update(models=[], status="untested", checked_at=None, message="Pendiente de comprobar")
        row.update(provider=payload.provider, label=payload.label.strip())
        write_store(rows)
    apply_catalog()
    return {"saved": True}


@router.delete("/{key_id}")
def delete_key(key_id: str):
    with LOCK:
        rows = read_store()
        kept = [row for row in rows if row["id"] != key_id]
        if len(kept) == len(rows):
            raise HTTPException(404, "Clave no encontrada")
        write_store(kept)
    apply_catalog()
    return {"deleted": True}


@router.post("/{key_id}/reveal")
def reveal_key(key_id: str):
    with LOCK:
        row = next((row for row in read_store() if row["id"] == key_id), None)
        if row is None:
            raise HTTPException(404, "Clave no encontrada")
        return {"secret": decrypt(row["secret"])}


@router.post("/{key_id}/test")
def test_key(key_id: str):
    with LOCK:
        row = next((row for row in read_store() if row["id"] == key_id), None)
        if row is None:
            raise HTTPException(404, "Clave no encontrada")
        encrypted = row["secret"]
        key = decrypt(encrypted)
    models = []
    selected = None
    try:
        models = discover(row["provider"], key)
        selected = preferred_model(row["provider"], models)
        if not selected:
            raise ValueError("Sin modelos de conversación")
        if row["provider"] == "gemini":
            response = requests.post("https://generativelanguage.googleapis.com/v1beta/models/" + selected + ":generateContent", headers={"x-goog-api-key": key}, json={"contents": [{"parts": [{"text": "Responde solamente OK"}]}], "generationConfig": {"maxOutputTokens": 64}}, timeout=25)
            response.raise_for_status()
            data = response.json()
            text = "".join(part.get("text", "") for candidate in data.get("candidates", []) for part in candidate.get("content", {}).get("parts", []))
            tokens = data.get("usageMetadata", {}).get("totalTokenCount", 0)
        else:
            response = requests.post(GLM_BASE + "/chat/completions", headers={"Authorization": "Bearer " + key}, json={"model": selected, "messages": [{"role": "user", "content": "Responde solamente OK"}], "max_tokens": 1024}, timeout=25)
            response.raise_for_status()
            data = response.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            tokens = data.get("usage", {}).get("total_tokens", 0)
        if not text.strip():
            raise ValueError("Respuesta vacía")
        from types import SimpleNamespace
        from agent_core.memory_vault import record_model_usage
        record_model_usage(("glm/" if row["provider"] == "glm" else "") + selected, SimpleNamespace(total_token_count=tokens))
        status, message = "working", "Consulta de prueba respondida correctamente"
    except requests.HTTPError as exc:
        code = exc.response.status_code
        status, message = {
            401: ("invalid", "Clave rechazada"),
            403: ("forbidden", "Sin permiso para este modelo o proyecto"),
            429: ("limited", "Sin cuota disponible o demasiadas solicitudes"),
            503: ("unavailable", "Proveedor saturado. Vuelve a comprobar más tarde"),
        }.get(code, ("error", f"La prueba falló (HTTP {code})"))
        if row["provider"] == "glm":
            try:
                provider_code = str(exc.response.json().get("error", {}).get("code", ""))
            except (ValueError, AttributeError):
                provider_code = ""
            if provider_code == "1305":
                status, message = "unavailable", "Modelo gratuito temporalmente saturado. La clave no fue rechazada; vuelve a comprobar en unos momentos"
            if provider_code == "1113":
                status, message = "limited", "Clave reconocida, pero tu cuenta no tiene saldo o un paquete de recursos para este modelo"
    except requests.RequestException:
        status, message = "offline", "No se pudo conectar con el proveedor"
    except ValueError:
        status, message = "error", "El proveedor no devolvió una respuesta de conversación válida"
    with LOCK:
        rows = read_store()
        current = next((item for item in rows if item["id"] == key_id), None)
        if current is None or current["secret"] != encrypted or current["provider"] != row["provider"]:
            raise HTTPException(409, "La clave cambió durante la prueba. Compruébala otra vez")
        current.update(status=status, message=message, models=models, checked_at=datetime.now(timezone.utc).isoformat(), test_model=selected)
        write_store(rows)
    apply_catalog()
    return {"ok": status == "working", "status": status, "message": message, "models": len(models), "test_model": selected}
