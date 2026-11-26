"""Generación local de archivos hecha para Vortex AI Desktop."""
import re
from pathlib import Path


def create_html_draft(message, folder):
    if not folder or not re.search(r"\b(?:crea|crear|creame|créame|haz|genera)\b", message, re.I):
        return None
    match = re.search(r"(?<![\w/\\.:~-])([a-zA-Z0-9_-]+\.html)\b", message, re.I)
    if not match:
        return None
    root = Path(folder).resolve()
    if not root.is_dir():
        return None
    target = root / match[1]
    if target.exists() or target.is_symlink():
        return None
    animated = bool(re.search(r"animaci[oó]n|motion|moci[oó]n", message, re.I))
    css = "body{margin:0;min-height:100vh;display:grid;place-items:center;background:#10152b;color:#eef2ff;font-family:system-ui}main{text-align:center;padding:2rem}.orb{width:130px;height:130px;margin:auto;border-radius:50%;background:linear-gradient(135deg,#6a5cff,#00d4ff);box-shadow:0 0 70px #6a5cff88}"
    if animated:
        css += ".orb{animation:float 3s ease-in-out infinite}@keyframes float{50%{transform:translateY(-24px) rotate(180deg);border-radius:30%}}@media(prefers-reduced-motion:reduce){.orb{animation:none}}"
    html = '<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Borrador inicial</title><style>' + css + '</style><main><div class="orb" aria-hidden="true"></div><h1>Borrador inicial</h1><p>Base local lista para continuar el diseño.</p></main></html>'
    try:
        with target.open("x", encoding="utf-8") as file:
            file.write(html)
    except OSError:
        return None
    return str(target)
