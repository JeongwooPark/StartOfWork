"""설치 폴더 잠금 방지용 TEMP 재기동 (1.2.12+: 앱 밖 설치로 기본 불필요)."""

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


def needs_temp_bootstrap(exe: Path) -> bool:
    """앱 설치 트리(StartOfWork\\Updater) 안에서만 TEMP 복사가 필요하다."""
    try:
        parts = [p.lower() for p in exe.resolve().parts]
        for i in range(len(parts) - 1):
            if parts[i] == "startofwork" and parts[i + 1] == "updater":
                return True
    except OSError:
        return False
    return False


def frozen_bundle_root(exe: Path) -> Path:
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
    """레거시(앱 안 Updater)만 TEMP 복사 재기동. 그 외는 -1."""
    if not getattr(sys, "frozen", False):
        return -1

    exe = Path(sys.executable)
    if is_running_from_temp(exe):
        return -1
    if not needs_temp_bootstrap(exe):
        return -1

    dest_exe = copy_bundle_to_temp(exe)
    args = [str(dest_exe), *argv]
    if "--bootstrapped" not in args:
        args.append("--bootstrapped")
    logging.info("업데이터 TEMP 재기동(레거시): %s", dest_exe)
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
