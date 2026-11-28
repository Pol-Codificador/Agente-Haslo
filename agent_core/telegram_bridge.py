"""Puente oficial de Telegram Bot API para Vortex AI Desktop."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import set_key, unset_key

from agent_core.config import ENV_PATH, get_env


_lock = threading.RLock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_generation = 0
_state: dict[str, Any] = {
    "status": "not_configured",
    "detail": "Agrega el token creado con @BotFather",
    "bot_username": "",
    "bot_name": "",
    "authorized_chat_id": "",
    "pending_chat_id": "",
    "pending_chat_name": "",
    "updated_at": "",
}


def _now() -> str:
    from datetime import datetime
    return datetime.now().astimezone().isoformat()


def _update(**values: Any) -> None:
    with _lock:
        _state.update(values, updated_at=_now())


def status() -> dict[str, Any]:
    with _lock:
        data = dict(_state)
    data["configured"] = bool(get_env("TELEGRAM_BOT_TOKEN", "").strip())
    data["running"] = bool(_thread and _thread.is_alive())
    data["authorized_chat_id"] = get_env("TELEGRAM_CHAT_ID", "").strip()
    return data


def _atomic_env(values: dict[str, str | None]) -> None:
    temporary = ENV_PATH.with_suffix(".telegram.pending")
    temporary.write_text(ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else "", encoding="utf-8")
    try:
        for key, value in values.items():
            if value is None:
                unset_key(str(temporary), key)
                os.environ.pop(key, None)
            else:
                set_key(str(temporary), key, value)
                os.environ[key] = value
        os.replace(temporary, ENV_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(token: str | None = None, chat_id: str | None = None) -> None:
    values: dict[str, str | None] = {}
    if token is not None:
        values["TELEGRAM_BOT_TOKEN"] = token.strip() or None
    if chat_id is not None:
        clean = chat_id.strip()
        if clean and not clean.lstrip("-").isdigit():
            raise ValueError("El Chat ID de Telegram debe contener solo números")
        values["TELEGRAM_CHAT_ID"] = clean or None
    if values:
        _atomic_env(values)


def _api(token: str, method: str, *, request_timeout: int = 35, **data: Any) -> Any:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        timeout=request_timeout,
    )
    payload = response.json()
    if not response.ok or not payload.get("ok"):
        raise RuntimeError(payload.get("description") or f"Telegram respondió HTTP {response.status_code}")
    return payload.get("result")


def validate_token(token: str) -> dict[str, Any]:
    bot = _api(token, "getMe", request_timeout=15)
    return {"id": bot.get("id"), "name": bot.get("first_name", ""), "username": bot.get("username", "")}


def _send_text(token: str, chat_id: str, text: str) -> None:
    content = (text or "Sin respuesta.").strip()
    while content:
        part = content[:4000]
        content = content[4000:]
        _api(token, "sendMessage", request_timeout=20, chat_id=chat_id, text=part)


def _send_files(token: str, chat_id: str, paths: list[str]) -> None:
    for raw_path in paths[:6]:
        path = Path(raw_path)
        if not path.is_file() or path.stat().st_size > 49 * 1024 * 1024:
            continue
        method = "sendPhoto" if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} else "sendDocument"
        field = "photo" if method == "sendPhoto" else "document"
        try:
            with path.open("rb") as handle:
                response = requests.post(
                    f"https://api.telegram.org/bot{token}/{method}",
                    data={"chat_id": chat_id}, files={field: (path.name, handle)}, timeout=60,
                )
                response.raise_for_status()
        except Exception:
            continue


def _handle_message(token: str, message: dict[str, Any]) -> None:
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not chat_id:
        return
    sender = message.get("from") or {}
    display = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip() or sender.get("username") or chat_id
    authorized = get_env("TELEGRAM_CHAT_ID", "").strip()
    if not authorized or chat_id != authorized:
        _update(pending_chat_id=chat_id, pending_chat_name=display, status="pending_authorization", detail=f"Chat detectado: {display}. Autorízalo desde Vortex.")
        _send_text(token, chat_id, "Vortex recibió tu mensaje. Autoriza este chat desde Telegram en la aplicación de escritorio.")
        return
    text = (message.get("text") or message.get("caption") or "").strip()
    if not text:
        _send_text(token, chat_id, "Por ahora envíame texto. El soporte de audio se añadirá después.")
        return
    if text == "/start":
        _send_text(token, chat_id, "Vortex AI Desktop conectado. Escríbeme una instrucción o usa /chats para ver tus conversaciones.")
        return
    try:
        response = requests.post(
            "http://127.0.0.1:8000/chat",
            json={"message": text, "sender": f"telegram:{chat_id}", "source": "telegram"},
            timeout=600,
        )
        response.raise_for_status()
        result = response.json()
        _send_text(token, chat_id, result.get("reply", ""))
        paths = list(result.get("images") or []) + list(result.get("files") or [])
        if result.get("image_path"):
            paths.insert(0, result["image_path"])
        _send_files(token, chat_id, paths)
    except Exception as exc:
        _send_text(token, chat_id, f"No pude procesar la solicitud: {exc}")


def _worker(generation: int) -> None:
    offset = 0
    while not _stop.is_set() and generation == _generation:
        token = get_env("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            _update(status="not_configured", detail="Agrega el token creado con @BotFather")
            return
        try:
            bot = validate_token(token)
            _update(status="connected", detail="Bot conectado y esperando mensajes", bot_username=bot["username"], bot_name=bot["name"])
            updates = _api(token, "getUpdates", request_timeout=40, offset=offset, timeout=25, allowed_updates='["message"]') or []
            for update in updates:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                if update.get("message"):
                    _handle_message(token, update["message"])
        except requests.RequestException as exc:
            _update(status="offline", detail=f"Sin conexión con Telegram: {exc}")
            _stop.wait(4)
        except Exception as exc:
            detail = str(exc)
            state = "invalid_token" if "Unauthorized" in detail else "error"
            _update(status=state, detail=detail)
            _stop.wait(4)


def start() -> dict[str, Any]:
    global _thread, _generation
    stop()
    if not get_env("TELEGRAM_BOT_TOKEN", "").strip():
        _update(status="not_configured", detail="Agrega el token creado con @BotFather")
        return status()
    _stop.clear()
    _generation += 1
    _thread = threading.Thread(target=_worker, args=(_generation,), name="vortex-telegram", daemon=True)
    _thread.start()
    return status()


def stop() -> None:
    global _generation
    _generation += 1
    _stop.set()
    thread = _thread
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=2)


def authorize_pending() -> dict[str, Any]:
    with _lock:
        chat_id = str(_state.get("pending_chat_id") or "")
    if not chat_id:
        raise ValueError("Aún no se detectó ningún chat. Envía /start al bot primero.")
    save_config(chat_id=chat_id)
    _update(status="connected", detail="Chat autorizado", pending_chat_id="", pending_chat_name="")
    return status()


def disconnect() -> dict[str, Any]:
    stop()
    save_config(token="", chat_id="")
    _update(status="not_configured", detail="Telegram desconectado", bot_username="", bot_name="", pending_chat_id="", pending_chat_name="")
    return status()
