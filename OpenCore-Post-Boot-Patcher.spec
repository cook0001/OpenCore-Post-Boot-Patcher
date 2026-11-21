# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

from PyInstaller.building.api import PYZ, EXE
from PyInstaller.building.build_main import Analysis

sys.path.append(os.path.abspath(os.getcwd()))

block_cipher = None

datas = []
if Path("payloads").is_dir():
    datas.append(("payloads", "payloads"))
elif Path("payloads.dmg").is_file():
    datas.append(("payloads.dmg", "."))

excludes = [
    "wx",
    "wxPython",
    "applescript",
    "AppKit",
    "Cocoa",
    "WebKit",
    "markdown2",
    "tkinter",
    "opencore_legacy_patcher.wx_gui",
]

a = Analysis(
    ["ocpbp_cli.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ocpbp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    target_arch="universal2",
    codesign_identity=None,
    entitlements_file=None,
)
