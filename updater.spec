# -*- mode: python ; coding: utf-8 -*-
"""독립형 StartOfWorkUpdater (onedir). tkinter + urllib 위주."""

from pathlib import Path

block_cipher = None
project_dir = Path(SPECPATH)

excludes = [
    "selenium",
    "pytest",
    "unittest",
    "doctest",
    "pdb",
    "pydoc",
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "IPython",
    "notebook",
    "pystray",
    "PIL",
]

hiddenimports = [
    "startofwork",
    "startofwork.constants",
    "startofwork.updater",
    "startofwork_updater",
    "startofwork_updater.app",
    "startofwork_updater.bootstrap",
    "startofwork_updater.install",
]

icon_file = project_dir / "StartOfWork.ico"

a = Analysis(
    ["updater_main.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[(str(icon_file), ".")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
    optimize=2,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StartOfWorkUpdater",
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
    icon=str(icon_file),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="StartOfWorkUpdater",
)
