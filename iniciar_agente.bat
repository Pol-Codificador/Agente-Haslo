@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No se encontro el entorno virtual .venv
    pause
    exit /b 1
)

if not exist "whatsapp_bridge\node_modules" (
    echo [ERROR] No se encontraron las dependencias de Node.js en whatsapp_bridge\node_modules
    pause
    exit /b 1
)

if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
    )
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".\.venv\Scripts\pythonw.exe" gui_launcher.py
) else (
    start "" ".\.venv\Scripts\python.exe" gui_launcher.py
)
