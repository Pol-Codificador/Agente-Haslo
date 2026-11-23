r"""Script de autenticación única OAuth2 para Google Classroom y Google Calendar.

Ejecutar una sola vez después de colocar `credentials.json` en la raíz del proyecto:
    .\.venv\Scripts\python.exe setup_google_auth.py
"""

from __future__ import annotations

import sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from agent_core.config import CREDENTIALS_PATH, GOOGLE_SCOPES, TOKEN_PATH, get_env


def print_credentials_instructions() -> None:
    """Muestra instrucciones paso a paso si falta credentials.json."""
    print("=" * 72)
    print("ERROR: No se encontró el archivo 'credentials.json' en:")
    print(f"  {CREDENTIALS_PATH}")
    print("=" * 72)
    print("Para generarlo sigue estos pasos en Google Cloud Console:")
    print("1. Ingresa a https://console.cloud.google.com/ y crea o selecciona un proyecto.")
    print("2. Ve a 'APIs y servicios' -> 'Biblioteca' y habilita:")
    print("   - Google Classroom API")
    print("   - Google Calendar API")
    print("3. Ve a 'APIs y servicios' -> 'Pantalla de consentimiento de OAuth':")
    print("   - Configúrala como 'Externo' (o Interno si tu cuenta Workspace lo permite).")
    print("   - Si es Externo en modo Prueba, agrega tu correo @unsa.edu.pe en 'Usuarios de prueba'.")
    print("4. Ve a 'Credenciales' -> 'Crear credenciales' -> 'ID de cliente de OAuth':")
    print("   - Tipo de aplicación: 'Aplicación de escritorio' (Desktop app).")
    print("5. Descarga el JSON, renómbralo a 'credentials.json' y colócalo en la raíz del proyecto.")
    print("=" * 72)


def main() -> int:
    """Ejecuta el flujo OAuth2 local y valida el acceso a Classroom y Calendar."""
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), GOOGLE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("Refrescando token de acceso existente...")
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                print_credentials_instructions()
                return 1

            print("Abriendo navegador para iniciar sesión con tu cuenta de Google (@unsa.edu.pe)...")
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH),
                GOOGLE_SCOPES,
            )
            creds = flow.run_local_server(port=0)

        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
        print(f"Token guardado correctamente en: {TOKEN_PATH}")
    else:
        print(f"Ya existe un token válido en: {TOKEN_PATH}")

    print("\n--- Verificando conexión con Google Classroom ---")
    try:
        classroom = build("classroom", "v1", credentials=creds)
        courses_resp = classroom.courses().list(courseStates=["ACTIVE"], pageSize=5).execute()
        courses = courses_resp.get("courses", [])
        if not courses:
            print("Conexión exitosa, no se encontraron cursos activos.")
        else:
            print(f"Conexión exitosa. Primeros cursos activos ({len(courses)}):")
            for c in courses:
                print(f"  - {c.get('name', 'Sin nombre')} (ID: {c.get('id')})")
    except Exception as exc:
        print(f"Advertencia al consultar Classroom: {exc}")

    print("\n--- Verificando conexión con Google Calendar ---")
    try:
        calendar_id = get_env("GOOGLE_CALENDAR_ID", "primary") or "primary"
        calendar = build("calendar", "v3", credentials=creds)
        cal_info = calendar.calendars().get(calendarId=calendar_id).execute()
        print(
            f"Conexión exitosa al calendario '{cal_info.get('summary', calendar_id)}' "
            f"(Zona horaria: {cal_info.get('timeZone', 'No definida')})"
        )
    except Exception as exc:
        print(f"Advertencia al consultar Calendar: {exc}")

    print("\n¡Autenticación completada!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
