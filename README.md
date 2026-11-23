# Agente-UNSA 🤖🎓

Asistente y agente personal autónomo impulsado por la **API de Google Gemini (`google-genai`)**, diseñado originalmente para estudiantes de la **Universidad Nacional de San Agustín (UNSA)** y usuarios de Windows que buscan integrar su vida académica y productividad con el control directo de su computadora desde **WhatsApp** o un **Dashboard Web local**.

Integra **Google Classroom**, **DUTIC (Moodle UNSA)**, **Google Drive**, **Google Calendar** y **WhatsApp** (texto, notas de voz e imágenes), con herramientas para interactuar directamente con la PC local: abrir aplicaciones, navegar enlaces, buscar y analizar archivos/PDFs, apoyar en programación, ejecutar comandos y programar alertas o automatizaciones.

---

> [!WARNING]
> **Proyecto en desarrollo activo (WIP) y aviso de uso responsable**
>
> `Agente-UNSA` se encuentra actualmente en **desarrollo activo** y **todavía no está pulido por completo** (puede presentar errores, comportamientos inesperados o dependencias específicas de Windows).
>
> **Úsalo con precaución y criterio:** este agente tiene capacidades reales sobre tu equipo local —puede ejecutar comandos de PowerShell, controlar mouse y teclado, leer/organizar archivos, operar repositorios Git, tomar capturas de pantalla o fotos de webcam y enviar mensajes de WhatsApp—. Aunque incluye capas de seguridad (confirmación explícita para acciones críticas, whitelist de número y revisión básica de privacidad en capturas), **siempre debes supervisar su uso y revisar la configuración antes de ejecutarlo en tu entorno**.
>
> Si te resulta útil, ¡siéntete libre de probarlo, adaptarlo y mejorarlo a tus necesidades!

---

## 1. Funcionalidades y Herramientas Implementadas

El agente expone **35 herramientas** mediante *Function Calling* de Gemini (con enrutamiento automático entre modelos rápidos y complejos, memoria conversacional reciente y modo multimodal):

### 🎓 Productividad Académica y UNSA
- **DUTIC UNSA (Moodle):** `get_dutic_events` lee entregas, cuestionarios y eventos próximos desde tu feed iCal (`.ics`).
- **Google Classroom y Drive:** `get_classroom_pending` lista cursos activos, tareas pendientes y anuncios; `download_classroom_attachment` descarga adjuntos de Drive directamente vía API sin abrir el navegador.
- **Google Calendar:** `get_calendar_schedule` consulta tu agenda diaria (zona horaria `America/Lima`) y calcula huecos libres; `create_calendar_event` agenda bloques de estudio o recordatorios con alarmas.
- **Análisis de documentos y videos:** `analyze_pdf_with_gemini` analiza PDFs locales de forma nativa con Gemini API; `summarize_youtube_video` extrae transcripciones con marcas de tiempo o metadatos de YouTube.
- **Asistencia con IA Web / Docs:** `ask_web_ai_in_brave`, `build_with_heavy_web_ai` y `create_google_doc_in_brave` permiten automatizar consultas complejas o generar documentos `.docx` estructurados desde Brave.

### 💻 Control de PC, Archivos y Programación
- **Estado y monitoreo del sistema:** `get_system_status` (CPU, RAM, disco, batería y Top 5 procesos), `take_screenshot` (soporte multimonitor inteligente) y `webcam_security_snapshot` (foto rápida con cámara web).
- **Terminal y desarrollo:** `run_terminal_command` (PowerShell con timeout de seguridad), `git_project_manager` (`status`, `commit`, `push`, `pull`, `log`, `diff`) y `run_claude_code_task` (delegación de tareas a Claude Code CLI).
- **Archivos y descargas:** `deep_search_in_files` (búsqueda dentro de PDFs, código y texto en Desktop/Downloads/Documents), `read_local_pdf_or_file`, `organize_or_inspect_folder`, `list_recent_downloads`, `wait_for_new_download` y `verify_file`.
- **Aplicaciones, ventanas y navegador:** `open_application_or_url`, `control_app_window` (VS Code, Brave, Antigravity), `activate_workspace_mode` (entornos completos de trabajo), `browse_and_capture` (Playwright/Chromium) y `open_and_automate_brave_service` (YouTube y servicios en Brave).
- **Interacción directa:** `gui_click_and_type` (mouse/teclado por coordenadas o visión), `media_control` (volumen, pausa/reproducción, bloqueo de pantalla) y `pc_clipboard_and_voice` (portapapeles y lectura en voz alta).

### 💬 WhatsApp, Notificaciones y Dashboard
- **Puente WhatsApp (`whatsapp_bridge/`):** Basado en `@whiskeysockets/baileys`, con whitelist estricta (`ALLOWED_PHONE_NUMBER`), soporte para mensajes de texto, notas de voz (`.ogg`), imágenes y envío de documentos (`send_file_to_whatsapp`, `send_whatsapp_to_contact`).
- **Alertas proactivas (`watchdog`):** `set_pc_or_task_alert` vigila eventos en segundo plano (batería, procesos, temporizadores, etc.) y te avisa automáticamente por WhatsApp.
- **Dashboard Cockpit Web:** Interfaz local en `http://127.0.0.1:8000` (`agent_core/dashboard.html`) para chatear con el agente, ver métricas de la PC, línea de tiempo en vivo, notas del Segundo Cerebro, contactos rápidos VIP y estado del puente de WhatsApp.

### 🧠 Memoria Permanente y Seguridad
- **Segundo Cerebro (SQLite):** `save_to_second_brain` y `search_second_brain` persisten notas, cursos, enlaces y recordatorios en `agent_core/memory_vault.db`.
- **Capas de seguridad (`agent_core/security.py`):**
  - Confirmación explícita antes de acciones críticas (`CONFIRM_CRITICAL_ACTIONS=true`: mensajes a terceros, comandos riesgosos, Git destructivo, cambios masivos de archivos).
  - Revisión básica de privacidad antes de enviar capturas al modelo (`SEND_SCREENSHOTS_TO_MODEL=true` y `assert_screen_privacy()`). *Limitación conocida:* en navegadores con varias pestañas, el título de ventana solo refleja la pestaña activa.
  - Aislamiento de contenido externo para mitigar *prompt injection*.
- **Servidor MCP opcional:** `agent_core/mcp_server.py` expone las herramientas mediante Model Context Protocol (`stdio`).

---

## 2. Arquitectura del Proyecto

```text
Agente-UNSA/
├── agent_core/
│   ├── agent.py          # Orquestador principal con Google Gemini API y enrutador de modelos
│   ├── config.py         # Carga de .env, rutas, scopes OAuth y estado/cooldown de modelos
│   ├── server.py         # Backend FastAPI (endpoints /chat, /audio, /reset y Dashboard API)
│   ├── dashboard.html    # Interfaz web local (Dashboard Cockpit)
│   ├── tools.py          # Herramientas de productividad, Windows, GUI, navegador y Git
│   ├── tools_drive.py    # Integración directa con Google Classroom/Drive y análisis de PDFs
│   ├── memory_vault.py   # Base de datos SQLite del Segundo Cerebro y confirmaciones
│   ├── security.py       # Guardias de privacidad y confirmación de acciones críticas
│   ├── watchdog.py       # Motor de alertas proactivas en segundo plano
│   └── mcp_server.py     # Servidor MCP (stdio) opcional
├── whatsapp_bridge/
│   ├── index.js          # Cliente WhatsApp con Baileys + servidor local de notificaciones
│   └── package.json      # Dependencias Node.js
├── gui_launcher.py       # Lanzador gráfico de escritorio para gestionar los servicios
├── iniciar_agente.bat    # Script de inicio rápido en Windows
├── setup_google_auth.py  # Flujo OAuth2 único para Google Classroom, Drive y Calendar
├── requirements.txt      # Dependencias de Python
└── .env.example          # Plantilla de configuración (sin secretos)
```

---

## 3. Requisitos Previos

- **Sistema Operativo:** Windows 10 u 11 (varias herramientas usan APIs nativas de Windows como `pywin32`, PowerShell y automatización de ventanas).
- **Python:** 3.11 o superior.
- **Node.js:** 18 o superior (con `npm`).
- **Clave de API de Google Gemini:** Obtenida desde [Google AI Studio](https://aistudio.google.com/app/apikey).
- *(Opcional)* **Proyecto en Google Cloud Console** si deseas integrar Google Classroom, Drive y Calendar.
- *(Opcional)* **Brave Browser** si utilizarás las herramientas de automatización en Brave.

---

## 4. Instalación Paso a Paso

### Paso 1: Clonar el repositorio
```powershell
git clone https://github.com/TU_USUARIO/Agente-UNSA.git
cd Agente-UNSA
```

### Paso 2: Crear el entorno virtual e instalar dependencias de Python
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
```

Si vas a usar navegación automatizada con Playwright (`browse_and_capture`), instala también el binario de Chromium:
```powershell
.\.venv\Scripts\playwright.exe install chromium
```

### Paso 3: Instalar dependencias del puente de WhatsApp (Node.js)
```powershell
cd whatsapp_bridge
npm install
cd ..
```

### Paso 4: Configurar variables de entorno (`.env`)
Copia la plantilla `.env.example` a `.env` y completa tus datos:
```powershell
copy .env.example .env
```

Variables principales en `.env`:
- `GEMINI_API_KEY`: Tu clave de API de Google AI Studio.
- `GEMINI_MODEL`, `GEMINI_MODELS_FAST`, `GEMINI_MODELS_COMPLEX`: Modelos de Gemini a utilizar en el enrutador.
- `INSTITUTIONAL_EMAIL`: Tu correo institucional (ej. `tu_correo@unsa.edu.pe`), usado como contexto del perfil UNSA.
- `DUTIC_ICAL_URL`: En DUTIC (Moodle) -> **Calendario** -> **Exportar calendario** -> *"Todos los eventos"* y *"Eventos próximos"* -> **Obtener URL del calendario**.
- `ALLOWED_PHONE_NUMBER`: Tu número de WhatsApp con código de país, sin `+` ni espacios (ej. `51987654321`). Solo este número podrá controlar al agente.
- `BOT_PREFIX`: Prefijo opcional (ej. `!` o `/bot`). Déjalo vacío para escribir directamente en tu chat personal.
- `GOOGLE_CALENDAR_ID`: `primary` (por defecto) o el ID de tu calendario.
- `AGENT_HOST`, `AGENT_PORT`, `WHATSAPP_NOTIFY_PORT`: Puertos locales (por defecto `127.0.0.1:8000` y `8001`).

### Paso 5: Autenticación con Google Classroom, Drive y Calendar *(Opcional)*
1. Ve a [Google Cloud Console](https://console.cloud.google.com/), crea un proyecto y habilita:
   - **Google Classroom API**
   - **Google Drive API**
   - **Google Calendar API**
2. Configura la **Pantalla de consentimiento de OAuth** (en modo Prueba, agrega tu correo en *Usuarios de prueba*).
3. En **Credenciales -> Crear credenciales -> ID de cliente de OAuth**, elige **Aplicación de escritorio**.
4. Descarga el JSON, renómbralo a **`credentials.json`** y colócalo en la raíz del proyecto.
5. Ejecuta una sola vez:
   ```powershell
   .\.venv\Scripts\python.exe setup_google_auth.py
   ```
   Se abrirá el navegador para autorizar tu cuenta y se generará `token.json` localmente (ignorado por Git).

---

## 5. Cómo Iniciar el Proyecto

### Opción A: Inicio rápido (Windows)
Haz doble clic en **`iniciar_agente.bat`** (o ejecútalo desde PowerShell). Esto abrirá el lanzador gráfico (`gui_launcher.py`) para gestionar el backend FastAPI y el puente de WhatsApp.

### Opción B: Inicio manual en dos terminales
- **Terminal 1 (Backend FastAPI + Dashboard):**
  ```powershell
  .\.venv\Scripts\python.exe -m agent_core.server
  ```
  El Dashboard estará disponible en `http://127.0.0.1:8000`.

- **Terminal 2 (Puente de WhatsApp):**
  ```powershell
  cd whatsapp_bridge
  npm start
  ```
  Escanea el **código QR** desde tu celular en **WhatsApp -> Dispositivos vinculados -> Vincular un dispositivo**.

---

## 6. Ejemplos de Uso (WhatsApp o Dashboard)

- *"¿Qué tareas tengo pendientes en DUTIC esta semana y en Classroom?"*
- *"Muéstrame mi horario de hoy y qué huecos libres tengo para estudiar."*
- *"Agenda un bloque de estudio de Base de Datos hoy de 18:00 a 20:00."*
- *"Descarga el PDF de la práctica de Redes de Classroom y resúmelo."*
- *"Busca en mis carpetas cualquier PDF que mencione 'normalización'."*
- *"Mándame una captura de pantalla de mi PC."*
- *"¿Cuánta RAM y batería me queda? ¿Qué procesos consumen más?"*
- *"Abre VS Code"* o *"Ejecuta `git status` en mi proyecto."*
- *"Guarda en mi segundo cerebro que el examen parcial es el viernes a las 10:00."*
- `/reset` para reiniciar la memoria reciente de la conversación.

---

## 7. Uso Opcional como Servidor MCP

`agent_core/mcp_server.py` expone las herramientas del agente vía **Model Context Protocol (`stdio`)**. Ejemplo de configuración para clientes compatibles:

```json
{
  "mcpServers": {
    "agente-unsa": {
      "command": "C:\\ruta\\a\\Agente-UNSA\\.venv\\Scripts\\python.exe",
      "args": ["-m", "agent_core.mcp_server"],
      "cwd": "C:\\ruta\\a\\Agente-UNSA"
    }
  }
}
```
