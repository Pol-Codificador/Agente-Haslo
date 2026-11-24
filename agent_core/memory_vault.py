"""Segundo Cerebro permanente en SQLite para notas, cursos, enlaces y recordatorios."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_core.config import BASE_DIR, LIMA_TZ, get_env

DB_PATH = BASE_DIR / "agent_core" / "memory_vault.db"
_DB_LOCK = threading.Lock()


def record_model_usage(model: str, usage: Any) -> None:
    """Registra tokens reportados por Gemini con persistencia entre reinicios."""
    if usage is None:
        return
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    with _DB_LOCK, _get_connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS model_usage (
            model TEXT NOT NULL, day TEXT NOT NULL, tokens INTEGER NOT NULL,
            requests INTEGER NOT NULL, PRIMARY KEY (model, day))""")
        conn.execute("""INSERT INTO model_usage VALUES (?, ?, ?, 1)
            ON CONFLICT(model, day) DO UPDATE SET
            tokens=tokens+excluded.tokens, requests=requests+1""",
            (model, day, int(getattr(usage, "total_token_count", 0) or 0)))


def get_model_usage() -> dict[str, Any]:
    from zoneinfo import ZoneInfo
    day = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    with _DB_LOCK, _get_connection() as conn:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='model_usage'").fetchone()
        rows = conn.execute("SELECT * FROM model_usage WHERE day=?", (day,)).fetchall() if exists else []
    return {row["model"]: dict(row) for row in rows}


def _get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    target = db_path or DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Crea las tablas `notes` y `pending_confirmations` si no existen todavía."""
    with _DB_LOCK:
        with _get_connection(db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT 'universidad',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_confirmations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender TEXT NOT NULL,
                    source TEXT NOT NULL,
                    action_name TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            institutional_email = get_env("INSTITUTIONAL_EMAIL", "")
            if institutional_email:
                existing_profile = conn.execute(
                    "SELECT id FROM notes WHERE LOWER(content) LIKE ? LIMIT 1",
                    (f"%{institutional_email.lower()}%",),
                ).fetchone()
                if existing_profile is None:
                    now_str = datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S")
                    conn.execute(
                        """
                        INSERT INTO notes (topic, content, tags, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "Perfil Institucional UNSA",
                            f"Correo institucional UNSA: {institutional_email}. Servicios Google UNSA suelen estar en /u/1/ y cuenta personal en /u/0/.",
                            "perfil, unsa, correo",
                            now_str,
                            now_str,
                        ),
                    )
            conn.commit()


def save_note(
    topic: str,
    content: str,
    tags: str = "universidad",
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Guarda una nueva nota permanente en SQLite y devuelve el registro creado."""
    clean_topic = (topic or "").strip() or "Nota general"
    clean_content = (content or "").strip()
    if not clean_content:
        raise ValueError("El contenido de la nota no puede estar vacío.")

    clean_tags = ", ".join(
        t.strip() for t in (tags or "universidad").split(",") if t.strip()
    ) or "universidad"
    now_str = datetime.now(LIMA_TZ).strftime("%Y-%m-%d %H:%M:%S")

    init_db(db_path)
    with _DB_LOCK:
        with _get_connection(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO notes (topic, content, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (clean_topic, clean_content, clean_tags, now_str, now_str),
            )
            conn.commit()
            note_id = int(cursor.lastrowid or 0)
            row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
            return dict(row) if row is not None else {
                "id": note_id,
                "topic": clean_topic,
                "content": clean_content,
                "tags": clean_tags,
                "created_at": now_str,
                "updated_at": now_str,
            }


def search_notes(
    query: str = "",
    limit: int = 30,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Busca notas por palabras clave en tema, contenido o etiquetas (o lista recientes si vacío)."""
    init_db(db_path)
    cleaned = (query or "").strip().lower()
    safe_limit = max(1, min(int(limit), 100))

    with _DB_LOCK:
        with _get_connection(db_path) as conn:
            if not cleaned or cleaned in ("*", "todo", "todos", "todas"):
                rows = conn.execute(
                    "SELECT * FROM notes ORDER BY id DESC LIMIT ?",
                    (safe_limit,),
                ).fetchall()
                return [dict(r) for r in rows]

            tokens = [tok for tok in cleaned.split() if tok]
            where_clauses = [
                "LOWER(topic || ' ' || content || ' ' || tags) LIKE ?"
                for _ in tokens
            ]
            params: list[Any] = [f"%{tok}%" for tok in tokens]
            params.append(safe_limit)
            sql = (
                "SELECT * FROM notes WHERE "
                + " AND ".join(where_clauses)
                + " ORDER BY id DESC LIMIT ?"
            )
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]


def delete_note(note_id: int, db_path: Path | None = None) -> bool:
    """Elimina una nota por su ID numérico."""
    init_db(db_path)
    with _DB_LOCK:
        with _get_connection(db_path) as conn:
            cur = conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
            conn.commit()
            return cur.rowcount > 0
