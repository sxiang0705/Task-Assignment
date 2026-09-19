# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


project_root = Path(SPECPATH).resolve()
oauth_client = project_root / "resources" / "google_oauth_client.json"
app_icon = project_root / "resources" / "icons" / "task_assignment.svg"
exe_icon = project_root / "resources" / "icons" / "task_assignment.ico"
packaged_data = [(str(app_icon), "resources/icons")]
packaged_data.append((str(exe_icon), "resources/icons"))
packaged_data.append((str(project_root / "resources" / "驗收說明.md"), "resources"))
if oauth_client.is_file():
    packaged_data.append((str(oauth_client), "resources"))

a = Analysis(
    [str(project_root / "src" / "task_assignment" / "app.py")],
    pathex=[str(project_root / "src")],
    binaries=[],
    datas=packaged_data,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "tools" / "startup_runtime_hook.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# Some development environments put Poppler's cross-platform ICU DLLs on PATH.
# Qt 6 on Windows intentionally links to the Windows ICU forwarding DLL in
# System32. PyInstaller must not collect Poppler's incompatible ``icuuc.dll``;
# doing so makes the frozen app fail while importing QtCore.
incompatible_icu_dlls = {"icuuc.dll", "icudt78.dll"}
a.binaries = [
    entry for entry in a.binaries if Path(entry[0]).name.lower() not in incompatible_icu_dlls
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="遺忘曲線大禮包 必上岸版本",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(exe_icon),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TaskAssignment",
)
