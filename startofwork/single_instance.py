"""Windows Named Mutex로 프로세스 단일 인스턴스 보장."""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

# 사용자 세션 범위 — 동일 Windows 로그인에서만 중복 실행 방지
MUTEX_NAME = "Local\\StartOfWork_SingleInstance_v1"
ERROR_ALREADY_EXISTS = 183

_mutex_handle: Optional[int] = None
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [
    wintypes.LPVOID,
    wintypes.BOOL,
    wintypes.LPCWSTR,
]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def try_acquire_single_instance(mutex_name: Optional[str] = None) -> bool:
    """
    이미 실행 중이면 False, 처음이면 Mutex를 보유하고 True.
    반환된 Mutex 핸들은 프로세스 종료까지 유지해야 한다.
    """
    global _mutex_handle

    if _mutex_handle:
        return True

    name = mutex_name or MUTEX_NAME
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        logging.error(
            "단일 인스턴스 Mutex 생성 실패 (err=%s)",
            ctypes.get_last_error(),
        )
        # Mutex 실패 시에도 실행은 허용 (가용성 우선)
        return True

    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        logging.warning("다른 StartOfWork 인스턴스가 이미 실행 중")
        return False

    _mutex_handle = int(handle)
    logging.info("단일 인스턴스 잠금 획득")
    return True


def release_single_instance() -> None:
    """테스트용 — 정상 종료 시 OS가 핸들을 정리하므로 필수 아님."""
    global _mutex_handle
    if _mutex_handle:
        try:
            _kernel32.CloseHandle(wintypes.HANDLE(_mutex_handle))
        except Exception:
            logging.exception("Mutex 해제 실패")
        _mutex_handle = None
