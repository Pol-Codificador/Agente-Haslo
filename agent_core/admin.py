"""Configuración local de conexiones. Módulo hecho para Vortex AI Desktop."""

import json
import os
import threading
from typing import Literal
from urllib.parse import urlparse

from dotenv import set_key, unset_key
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
GOOGLE_CONNECTION_META_PATH = CREDENTIALS_PATH.with_name("google_connection.json")
FIELDS = ("INSTITUTIONAL_EMAIL", "ALLOWED_PHONE_NUMBER", "BOT_PREFIX", "GOOGLE_CALENDAR_ID")


class WebGenerationSettings(BaseModel):
    enabled: bool = False
    browser: Literal["brave", "chrome", "edge"] = "brave"


@router.get("/web-generation")
def read_web_generation():
    return {"enabled": get_env("ALLOW_WEB_GENERATION", "false").lower() == "true", "browser": get_env("WEB_AI_BROWSER", "brave")}


@router.post("/web-generation")
def save_web_generation(payload: WebGenerationSettings):
    values = {"ALLOW_WEB_GENERATION": str(payload.enabled).lower(), "WEB_AI_BROWSER": payload.browser}
    with _lock:
        temporary = ENV_PATH.with_suffix(".pending")
        try:
            temporary.write_text(ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else "", encoding="utf-8")
            for key, value in values.items():
                set_key(str(temporary), key, value)
            os.replace(temporary, ENV_PATH)
            os.environ.update(values)
        finally:
            temporary.unlink(missing_ok=True)
    return {"saved": True, **payload.model_dump()}


class ConnectionSettings(BaseModel):
    institutional_email: str = Field(default="", max_length=254)
    allowed_phone_number: str = Field(default="", max_length=20)
    bot_prefix: str = Field(default="", max_length=40)
    google_calendar_id: str = Field(default="primary", max_length=300)
    dutic_url: str = Field(default="", max_length=4000)



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
            "google_connection": google_connection_info(),
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
    if payload.dutic_url.strip():
        values["DUTIC_ICAL_URL"] = payload.dutic_url.strip()
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
    return {"saved": True, "restart_whatsapp": old_phone != phone or old_prefix != payload.bot_prefix.strip()}


@router.delete("/dutic")
def remove_dutic_connection():
    with _lock:
        if ENV_PATH.exists():
            temporary = ENV_PATH.with_suffix(".pending")
            try:
                temporary.write_text(ENV_PATH.read_text(encoding="utf-8"), encoding="utf-8")
                unset_key(str(temporary), "DUTIC_ICAL_URL")
                os.replace(temporary, ENV_PATH)
            finally:
                temporary.unlink(missing_ok=True)
        os.environ.pop("DUTIC_ICAL_URL", None)
    return {"removed": True, "dutic_configured": False}


def get_gemini_keys() -> list[str]:
    from agent_core.provider_keys import credentials
    return sorted([key for _, key, _ in credentials("gemini")], key=lambda key: key != _preferred_key)


def prefer_gemini_key(key: str) -> None:
    global _preferred_key
    _preferred_key = key


@router.post("/gemini/test")
def test_gemini():
    from agent_core.provider_keys import credentials, test_key
    keys = credentials("gemini")
    if not keys:
        raise HTTPException(400, "Agrega una clave de Gemini en Configuración")
    return test_key(keys[0][0])


class GoogleCredentials(BaseModel):
    document: dict
    filename: str = Field(default="credentials.json", max_length=255)


def google_connection_info() -> dict:
    metadata = {}
    try:
        if GOOGLE_CONNECTION_META_PATH.exists():
            metadata = json.loads(GOOGLE_CONNECTION_META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        metadata = {}
    return {
        "authorized": TOKEN_PATH.exists(),
        "credentials_uploaded": CREDENTIALS_PATH.exists(),
        "credential_file": str(metadata.get("filename") or ("credentials.json" if CREDENTIALS_PATH.exists() else "")),
        "account": get_env("INSTITUTIONAL_EMAIL", ""),
    }


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
        safe_name = os.path.basename(payload.filename.strip()) or "credentials.json"
        GOOGLE_CONNECTION_META_PATH.write_text(json.dumps({"filename": safe_name}), encoding="utf-8")
    return {"saved": True}


@router.post("/google/disconnect")
def disconnect_google():
    with _lock:
        TOKEN_PATH.unlink(missing_ok=True)
        _auth_state.update(status="idle", message="Cuenta Google desconectada")
    return {"disconnected": True, **google_connection_info()}


@router.delete("/google/connection")
def remove_google_connection():
    with _lock:
        TOKEN_PATH.unlink(missing_ok=True)
        CREDENTIALS_PATH.unlink(missing_ok=True)
        GOOGLE_CONNECTION_META_PATH.unlink(missing_ok=True)
        _auth_state.update(status="idle", message="Conexión Google eliminada")
    return {"removed": True, **google_connection_info()}


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
