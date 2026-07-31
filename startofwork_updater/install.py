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


def _image_running(image_name: str) -> bool:
    check = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/NH"],
        capture_output=True,
        text=True,
        check=False,
    )
    return image_name.lower() in (check.stdout or "").lower()


def _taskkill_image(image_name: str, *, log_path: Path) -> None:
    if not _image_running(image_name):
        return
    result = subprocess.run(
        ["taskkill", "/F", "/IM", image_name, "/T"],
        capture_output=True,
        text=True,
        check=False,
    )
    write_update_log(
        log_path,
        f"taskkill {image_name} exit={result.returncode} "
        f"out={(result.stdout or '').strip()} err={(result.stderr or '').strip()}",
    )


def _kill_chrome_using_startofwork_profile(*, log_path: Path) -> None:
    """Selenium이 남긴 Chrome이 chrome_profile을 잠그면 Setup이 실패한다."""
    if sys.platform != "win32":
        return
    # StartOfWork 전용 프로필 경로를 쓰는 chrome.exe만 종료
    ps = (
        "$procs = Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" "
        "-ErrorAction SilentlyContinue; "
        "foreach ($p in $procs) { "
        "  if ($p.CommandLine -and ($p.CommandLine -like '*StartOfWork*chrome_profile*')) { "
        "    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; "
        "      Write-Output \"killed chrome pid=$($p.ProcessId)\" } "
        "    catch { Write-Output \"kill chrome pid=$($p.ProcessId) failed: $_\" } "
        "  } "
        "}"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", ps],
        capture_output=True,
        text=True,
        check=False,
    )
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out or err or result.returncode != 0:
        write_update_log(
            log_path,
            f"chrome profile cleanup exit={result.returncode} out={out} err={err}",
        )


def terminate_main_app(*, target_pid: int, log_path: Path) -> None:
    """지정 PID와 StartOfWork.exe·관련 브라우저를 종료한다."""
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
            write_update_log(
                log_path,
                f"OpenProcess failed for pid={pid} (already exited?)",
            )
            return
        try:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.WaitForSingleObject(handle, 15000)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    if target_pid > 0:
        _terminate_pid(target_pid)

    # PID 종료 후에도 남아 있을 때만 taskkill
    if _image_running("StartOfWork.exe"):
        _taskkill_image("StartOfWork.exe", log_path=log_path)
    else:
        write_update_log(
            log_path, "no StartOfWork.exe remaining after TerminateProcess"
        )

    # 고아 chromedriver / 전용 프로필 Chrome이 설치 폴더 파일을 잠글 수 있음
    _taskkill_image("chromedriver.exe", log_path=log_path)
    _kill_chrome_using_startofwork_profile(log_path=log_path)

    # 프로세스가 실제로 사라질 때까지 짧게 대기
    deadline = time.time() + 10
    while time.time() < deadline and _image_running("StartOfWork.exe"):
        time.sleep(0.4)
    time.sleep(1.0)
    write_update_log(
        log_path,
        "main process terminate done "
        f"startofwork_running={_image_running('StartOfWork.exe')}",
    )


def setup_installer_args(setup_path: Path) -> list[str]:
    """업데이터가 이미 메인을 종료했으므로 CloseApplications를 끈다.

    /CLOSEAPPLICATIONS + /SUPPRESSMSGBOXES 조합은 종료할 앱이 없거나
    Chrome 등이 응답하지 않으면 Abort(exit=5)로 끝난다.
    """
    return [
        str(setup_path.resolve()),
        "/SILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/NOCLOSEAPPLICATIONS",
    ]


def run_setup_installer(setup_path: Path, *, log_path: Path) -> int:
    setup_path = setup_path.resolve()
    if not setup_path.is_file():
        write_update_log(log_path, f"setup missing: {setup_path}")
        raise FileNotFoundError(str(setup_path))

    args = setup_installer_args(setup_path)
    write_update_log(log_path, f"launching setup={' '.join(args)}")
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
