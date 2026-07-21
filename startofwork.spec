# -*- mode: python ; coding: utf-8 -*-
"""Windows 실행 파일 빌드 설정 (onedir).

onefile은 실행마다 %TEMP%\\_MEIxxxx 에 DLL을 풀어 Smart App Control에
막히기 쉬우므로, 설치 폴더에 런타임을 두는 onedir을 사용한다.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
project_dir = Path(SPECPATH)

excludes = [
    "selenium.webdriver.firefox",
    "selenium.webdriver.edge",
    "selenium.webdriver.safari",
    "selenium.webdriver.ie",
    "selenium.webdriver.webkitgtk",
    "selenium.webdriver.wpewebkit",
    "pytest",
    "unittest",
    "doctest",
    "pdb",
    "pydoc",
    "tkinter.test",
    "xmlrpc",
    "sqlite3",
    "numpy",
    "pandas",
    "scipy",
    "matplotlib",
    "IPython",
    "notebook",
    "PIL.ImageTk",
    "PIL.ImageQt",
    "PIL.PdfImagePlugin",
    "PIL.MpegImagePlugin",
    "PIL.EpsImagePlugin",
    "PIL.SpiderImagePlugin",
]

hiddenimports = [
    "startofwork",
    "startofwork.app",
    "startofwork.gui",
    "startofwork.browser",
    "startofwork.config",
    "startofwork.holidays",
    "startofwork.attendance_state",
    "startofwork.rules",
    "startofwork.lock_state",
    "startofwork.paths",
    "startofwork.constants",
    "startofwork.single_instance",
    "startofwork.notifications",
    "startofwork.updater",
    "pystray._win32",
    "PIL.IcoImagePlugin",
    "PIL.PngImagePlugin",
    *collect_submodules("selenium.webdriver.chrome"),
    *collect_submodules("selenium.webdriver.chromium"),
    *collect_submodules("selenium.webdriver.common"),
    *collect_submodules("selenium.webdriver.remote"),
    "selenium.webdriver.chrome.webdriver",
    "selenium.webdriver.chromium.webdriver",
]

icon_file = project_dir / "StartOfWork.ico"

a = Analysis(
    ["main.py"],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[
        (str(icon_file), "."),
        (str(project_dir / "config.example.json"), "."),
    ],
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


def _should_drop(name: str) -> bool:
    lowered = name.lower().replace("\\", "/")
    drop_tokens = (
        "/firefox/",
        "geckodriver",
        "msedgedriver",
        "edgedriver",
        "safaridriver",
        "iedriver",
        "pytest",
        "unittest",
    )
    return any(token in lowered for token in drop_tokens)


a.binaries = [b for b in a.binaries if not _should_drop(b[0])]
a.datas = [d for d in a.datas if not _should_drop(d[0])]

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StartOfWork",
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
    name="StartOfWork",
)
