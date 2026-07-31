"""설치 폴더 잠금 방지를 위해 TEMP로 onedir을 복사한 뒤 재기동."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path


def updater_temp_dir() -> Path:
    path = (
        Path(os.environ.get("TEMP", os.environ.get("TMP", ".")))
        / "StartOfWorkUpdate"
        / "Updater"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_running_from_temp(exe: Path) -> bool:
    try:
        resolved = exe.resolve()
        temp_root = updater_temp_dir().resolve()
        return temp_root in resolved.parents or resolved.parent == temp_root
    except OSError:
        return False


def frozen_bundle_root(exe: Path) -> Path:
    """PyInstaller onedir: exe 옆 _internal 이 있으면 그 폴더가 번들 루트."""
    return exe.resolve().parent


def copy_bundle_to_temp(exe: Path) -> Path:
    src_root = frozen_bundle_root(exe)
    dest_root = updater_temp_dir()
    if dest_root.exists():
        shutil.rmtree(dest_root, ignore_errors=True)
    dest_root.mkdir(parents=True, exist_ok=True)
    for item in src_root.iterdir():
        target = dest_root / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            shutil.copy2(item, target)
    dest_exe = dest_root / exe.name
    if not dest_exe.is_file():
        raise RuntimeError(f"TEMP 업데이터 복사 실패: {dest_exe}")
    return dest_exe


def relaunch_from_temp(argv: list[str]) -> int:
    """설치본이면 TEMP 복사본으로 재실행 후 0. 이미 TEMP/개발 실행이면 -1."""
    if not getattr(sys, "frozen", False):
        return -1

    exe = Path(sys.executable)
    if is_running_from_temp(exe):
        return -1

    dest_exe = copy_bundle_to_temp(exe)
    args = [str(dest_exe), *argv]
    if "--bootstrapped" not in args:
        args.append("--bootstrapped")
    logging.info("업데이터 TEMP 재기동: %s", dest_exe)
    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    subprocess.Popen(
        args,
        cwd=str(dest_exe.parent),
        creationflags=creationflags,
        close_fds=True,
    )
    return 0
