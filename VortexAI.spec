from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH)
node_exe = Path(r"C:\Program Files\nodejs\node.exe")
hidden = collect_submodules("uvicorn") + collect_submodules("google")

a = Analysis(
    [str(root / "desktop_app.py")],
    pathex=[str(root)],
    binaries=[(str(node_exe), ".")] if node_exe.exists() else [],
    datas=[
        (str(root / ".env.example"), "."),
        (str(root / "agent_core" / "dashboard.html"), "agent_core"),
        (str(root / "agent_core" / "admin.css"), "agent_core"),
        (str(root / "agent_core" / "admin.js"), "agent_core"),
        (str(root / "agent_core" / "keys.js"), "agent_core"),
        (str(root / "agent_core" / "whatsapp.js"), "agent_core"),
        (str(root / "assets"), "assets"),
        (str(root / "whatsapp_bridge"), "whatsapp_bridge"),
    ],
    hiddenimports=hidden,
    hookspath=[],
    excludes=["tkinter", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="VortexAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    name="VortexAI",
)
