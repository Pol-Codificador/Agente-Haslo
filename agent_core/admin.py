"""Configuración local de conexiones desde el panel de administración."""

import json
import os
import threading
from urllib.parse import urlparse

from dotenv import set_key
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from agent_core.config import CREDENTIALS_PATH, ENV_PATH, GOOGLE_SCOPES, TOKEN_PATH, get_env


def require_local(request: Request) -> None:
    if request.client.host not in ("127.0.0.1", "::1", "testclient"):
        raise HTTPException(403, "La configuración requiere acceso local")
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Origen no permitido")


router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_local)])
_lock = threading.Lock()
_auth_state = {"status": "idle", "message": ""}
_preferred_key = ""
FIELDS = ("INSTITUTIONAL_EMAIL", "ALLOWED_PHONE_NUMBER", "BOT_PREFIX", "GOOGLE_CALENDAR_ID")


class ConnectionSettings(BaseModel):
    gemini_key: str = Field(default="", max_length=1000)
    institutional_email: str = Field(default="", max_length=254)
    allowed_phone_number: str = Field(default="", max_length=20)
    bot_prefix: str = Field(default="", max_length=40)
    google_calendar_id: str = Field(default="primary", max_length=300)
    dutic_url: str = Field(default="", max_length=4000)
    gemini_keys: list[str] | None = Field(default=None, max_length=20)
    replace_keys: bool = False
    gemini_model: str = Field(default="", max_length=120)
    gemini_models_fast: str = Field(default="", max_length=2000)
    gemini_models_complex: str = Field(default="", max_length=2000)


@router.get("/settings")
def read_settings():
    with _lock:
        values = {key.lower(): get_env(key) for key in FIELDS}
        if not values["allowed_phone_number"].isdigit():
            values["allowed_phone_number"] = ""
        if values["institutional_email"] == "tu_correo@unsa.edu.pe":
            values["institutional_email"] = ""
        return {
            "values": values,
            "gemini_configured": bool(get_env("GEMINI_API_KEY")),
            "dutic_configured": bool(get_env("DUTIC_ICAL_URL")),
            "credentials_uploaded": CREDENTIALS_PATH.exists(),
            "google_connected": TOKEN_PATH.exists(),
            "google_auth": dict(_auth_state),
            "key_count": len(get_gemini_keys()),
            "models": {name.lower(): get_env(name) for name in ("GEMINI_MODEL", "GEMINI_MODELS_FAST", "GEMINI_MODELS_COMPLEX")},
        }


@router.post("/settings")
def save_settings(payload: ConnectionSettings):
    phone = payload.allowed_phone_number.strip()
    if phone and (not phone.isdigit() or not 8 <= len(phone) <= 15):
        raise HTTPException(422, "Ingresa el número con código de país, solo dígitos")
    if any("\n" in value or "\r" in value for value in payload.model_dump().values() if isinstance(value, str)):
        raise HTTPException(422, "Los campos deben ocupar una sola línea")
    if payload.dutic_url and urlparse(payload.dutic_url).scheme not in ("https", "http"):
        raise HTTPException(422, "La URL del calendario debe comenzar con https:// o http://")
    values = {key: getattr(payload, key.lower()).strip() for key in FIELDS}
    if payload.gemini_key.strip():
        values["GEMINI_API_KEY"] = payload.gemini_key.strip()
    if payload.dutic_url.strip():
        values["DUTIC_ICAL_URL"] = payload.dutic_url.strip()
    if payload.gemini_keys is not None:
        keys = list(dict.fromkeys(key.strip() for key in payload.gemini_keys if key.strip()))
        if not keys or any(len(key) > 1000 or any(c.isspace() for c in key) for key in keys):
            raise HTTPException(422, "Agrega al menos una clave válida, una por línea")
        if not payload.replace_keys:
            keys = list(dict.fromkeys([*get_gemini_keys(), *keys]))
        values["GEMINI_API_KEYS"] = json.dumps(keys)
        values["GEMINI_API_KEY"] = keys[0]
    for name in ("GEMINI_MODEL", "GEMINI_MODELS_FAST", "GEMINI_MODELS_COMPLEX"):
        value = getattr(payload, name.lower()).strip()
        if value:
            values[name] = value
    with _lock:
        old_phone, old_prefix = get_env("ALLOWED_PHONE_NUMBER"), get_env("BOT_PREFIX")
        # Reemplazo atómico del archivo completo, conservando los demás ajustes.
        temporary = ENV_PATH.with_suffix(".pending")
        try:
            temporary.write_text(ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else "", encoding="utf-8")
            for key, value in values.items():
                set_key(str(temporary), key, value)
            os.replace(temporary, ENV_PATH)
        finally:
            temporary.unlink(missing_ok=True)
        os.environ.update(values)
        from agent_core import config
        for name in ("GEMINI_MODELS_FAST", "GEMINI_MODELS_COMPLEX"):
            if name in values:
                getattr(config, name)[:] = list(dict.fromkeys(part.strip() for part in values[name].split(",") if part.strip()))
        if "GEMINI_MODEL" in values:
            # Los módulos existentes importan este valor por nombre.
            import sys
            for name in ("agent_core.config", "agent_core.agent", "agent_core.tools", "agent_core.tools_drive", "agent_core.server"):
                module = sys.modules.get(name)
                if module:
                    module.GEMINI_MODEL = values["GEMINI_MODEL"]
    return {"saved": True, "restart_whatsapp": old_phone != phone or old_prefix != payload.bot_prefix.strip()}


def get_gemini_keys() -> list[str]:
    try:
        keys = json.loads(get_env("GEMINI_API_KEYS", "[]"))
        if isinstance(keys, list) and keys:
            unique = list(dict.fromkeys(str(key) for key in keys if key))
            return sorted(unique, key=lambda key: key != _preferred_key)
    except ValueError:
        pass
    key = get_env("GEMINI_API_KEY")
    return [key] if key else []


def prefer_gemini_key(key: str) -> None:
    global _preferred_key
    _preferred_key = key


@router.post("/gemini/test")
def test_gemini():
    from google import genai
    try:
        client = genai.Client(api_key=get_env("GEMINI_API_KEY"), http_options={"timeout": 20000})
        with client:
            models = list(client.models.list())
        return {"ok": True, "message": f"Clave aceptada. {len(models)} modelos visibles en el catálogo."}
    except Exception:
        raise HTTPException(400, "No se pudo validar la clave. Revisa la clave, la conexión y el acceso al proyecto.")


class GoogleCredentials(BaseModel):
    document: dict


@router.post("/google/credentials")
def upload_credentials(payload: GoogleCredentials):
    installed = payload.document.get("installed", {})
    if not all(installed.get(key) for key in ("client_id", "client_secret", "auth_uri", "token_uri")):
        raise HTTPException(422, "Selecciona el JSON OAuth de una aplicación de escritorio de Google")
    if installed["auth_uri"] != "https://accounts.google.com/o/oauth2/auth" or installed["token_uri"] != "https://oauth2.googleapis.com/token":
        raise HTTPException(422, "El archivo debe usar los servidores OAuth oficiales de Google")
    with _lock:
        if _auth_state["status"] == "pending":
            raise HTTPException(409, "Hay una autorización en curso")
        CREDENTIALS_PATH.write_text(json.dumps(payload.document), encoding="utf-8")
    return {"saved": True}


@router.post("/google/connect")
def connect_google():
    with _lock:
        if not CREDENTIALS_PATH.exists():
            raise HTTPException(400, "Primero carga el archivo de credenciales OAuth")
        if _auth_state["status"] == "pending":
            return dict(_auth_state)
        _auth_state.update(status="pending", message="Completa la autorización en el navegador de Google")

    def authorize():
        from google_auth_oauthlib.flow import InstalledAppFlow
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), GOOGLE_SCOPES)
            credentials = flow.run_local_server(port=0, timeout_seconds=180, authorization_prompt_message="", success_message="Cuenta conectada. Puedes cerrar esta ventana.")
            if credentials is None:
                raise ValueError("Autorización incompleta")
            with _lock:
                TOKEN_PATH.write_text(credentials.to_json(), encoding="utf-8")
                _auth_state.update(status="connected", message="Cuenta Google autorizada")
        except Exception:
            with _lock:
                _auth_state.update(status="error", message="Autorización incompleta. Puedes volver a intentarlo.")

    threading.Thread(target=authorize, daemon=True).start()
    return dict(_auth_state)
