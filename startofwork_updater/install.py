"""메인 프로세스 종료 및 Setup 실행."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path


def write_update_log(log_path: Path, message: str) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(line)


def terminate_main_app(*, target_pid: int, log_path: Path) -> None:
    """지정 PID와 StartOfWork.exe 프로세스를 종료한다."""
    write_update_log(log_path, f"terminating main pid={target_pid}")
    if sys.platform != "win32":
        return

    import ctypes

    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000

    def _terminate_pid(pid: int) -> None:
        if pid <= 0 or pid == os.getpid():
            return
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_TERMINATE | SYNCHRONIZE, False, pid
        )
        if not handle:
            return
        try:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.WaitForSingleObject(handle, 15000)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    if target_pid > 0:
        _terminate_pid(target_pid)

    # PID 종료 후에도 남아 있을 때만 taskkill
    check = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq StartOfWork.exe", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    if "StartOfWork.exe" in (check.stdout or ""):
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "StartOfWork.exe", "/T"],
            capture_output=True,
            text=True,
            check=False,
        )
        write_update_log(
            log_path,
            f"taskkill StartOfWork.exe exit={result.returncode} "
            f"out={(result.stdout or '').strip()} err={(result.stderr or '').strip()}",
        )
    else:
        write_update_log(log_path, "no StartOfWork.exe remaining after TerminateProcess")
    time.sleep(2)
    write_update_log(log_path, "main process terminate done")


def run_setup_installer(setup_path: Path, *, log_path: Path) -> int:
    setup_path = setup_path.resolve()
    if not setup_path.is_file():
        write_update_log(log_path, f"setup missing: {setup_path}")
        raise FileNotFoundError(str(setup_path))

    write_update_log(log_path, f"launching setup={setup_path}")
    args = [
        str(setup_path),
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
    ]
    completed = subprocess.run(
        args,
        cwd=str(setup_path.parent),
        check=False,
    )
    write_update_log(log_path, f"setup exitCode={completed.returncode}")
    return int(completed.returncode)


def restart_main_app(install_exe: Path, *, log_path: Path) -> None:
    install_exe = install_exe.resolve()
    if not install_exe.is_file():
        write_update_log(log_path, f"installed exe missing: {install_exe}")
        return
    write_update_log(log_path, f"restarting app={install_exe}")
    if sys.platform == "win32":
        import ctypes

        rc = int(
            ctypes.windll.shell32.ShellExecuteW(
                None,
                "open",
                str(install_exe),
                None,
                str(install_exe.parent),
                1,  # SW_SHOWNORMAL
            )
        )
        write_update_log(log_path, f"ShellExecute restart rc={rc}")
    else:
        subprocess.Popen([str(install_exe)], cwd=str(install_exe.parent))


def unblock_file(path: Path) -> None:
    if sys.platform != "win32":
        return
    try:
        ads = f"{path}:Zone.Identifier"
        if os.path.exists(ads):
            os.remove(ads)
    except OSError:
        pass
    try:
        quoted = str(path).replace("'", "''")
        subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Unblock-File -LiteralPath '{quoted}'",
            ],
            capture_output=True,
            check=False,
        )
    except OSError:
        pass
