"""출근/퇴근 완료 알림 (트레이 알림 — PowerShell 미사용).

Windows 11 Smart App Control은 서명되지 않은 PowerShell 스크립트·하위 프로세스를
'앱의 일부'로 차단할 수 있어, 토스트용 powershell.exe 호출은 사용하지 않는다.
이미 상주 중인 pystray 트레이 아이콘의 notify(풍선/알림)로 표시한다.
"""

from __future__ import annotations

import logging
import threading
from datetime import date
from typing import Callable, Optional

from startofwork.constants import APP_TITLE

# (title, message) → UI/트레이 스레드에서 표시
NotificationHandler = Callable[[str, str], None]

_handler: Optional[NotificationHandler] = None
_handler_lock = threading.Lock()


def set_notification_handler(handler: Optional[NotificationHandler]) -> None:
    """GUI가 알림 표시 핸들러를 등록/해제한다."""
    global _handler
    with _handler_lock:
        _handler = handler


def show_windows_toast(title: str, message: str) -> None:
    """등록된 핸들러로 알림을 표시한다. 실패해도 본 작업은 막지 않는다."""
    with _handler_lock:
        handler = _handler
    if handler is None:
        logging.info("알림 핸들러 없음 — 생략: %s", title)
        return
    try:
        handler(title, message)
        logging.info("알림 표시 요청: %s — %s", title, message)
    except Exception:
        logging.exception("알림 표시 실패")


def notify_check_in_done(day: Optional[date] = None) -> None:
    target = day or date.today()
    show_windows_toast(
        "출근 체크 완료",
        f"{APP_TITLE} — {target.isoformat()} 출근이 처리되었습니다.",
    )


def notify_check_out_done(day: Optional[date] = None) -> None:
    target = day or date.today()
    show_windows_toast(
        "퇴근 체크 완료",
        f"{APP_TITLE} — {target.isoformat()} 퇴근이 처리되었습니다.",
    )


def notify_attendance_failure(
    *,
    title: str,
    message: str,
) -> None:
    show_windows_toast(title, f"{APP_TITLE} — {message}")
