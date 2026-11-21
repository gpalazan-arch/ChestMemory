# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for building standalone Windows ChestMemory.exe."""

from PyInstaller.utils.hooks import collect_submodules

hidden_imports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtNetwork",
    "cv2",
    "numpy",
    "mss",
    "watchdog.observers.read_directory_changes",
    "cryptography.hazmat.primitives.ciphers",
] + collect_submodules("watchdog")

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=[("data", "data")],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "IPython"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ChestMemory",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
