"""애플리케이션 엔트리포인트."""

from __future__ import annotations

import ctypes
import logging
import platform
import sys

from startofwork.constants import APP_TITLE
from startofwork.config import ensure_app_config
from startofwork.gui import LockStateMonitor
from startofwork.paths import LOG_FILE, setup_logging
from startofwork.single_instance import try_acquire_single_instance


def _show_already_running() -> None:
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "StartOfWork가 이미 실행 중입니다.\n\n"
            "시스템 트레이를 확인하거나, 기존 프로그램을 종료한 뒤 다시 실행하세요.",
            APP_TITLE,
            0x30,  # MB_ICONWARNING
        )
    except Exception:
        pass


def main() -> None:
    setup_logging()
    if platform.system() != "Windows":
        raise OSError("이 프로그램은 Windows에서만 실행할 수 있습니다")

    if not try_acquire_single_instance():
        logging.warning("중복 실행 감지 — 종료")
        _show_already_running()
        sys.exit(0)

    ensure_app_config()

    app = LockStateMonitor()
    app.mainloop()


def run() -> None:
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        logging.exception("프로그램에서 처리되지 않은 오류 발생")
        if platform.system() == "Windows":
            try:
                ctypes.windll.user32.MessageBoxW(
                    None,
                    f"프로그램 실행 중 오류가 발생했습니다.\n\n로그 파일:\n{LOG_FILE}",
                    APP_TITLE,
                    0x10,
                )
            except Exception:
                pass


if __name__ == "__main__":
    run()
