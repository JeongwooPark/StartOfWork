"""Windows 세션 잠금 상태 조회."""

from __future__ import annotations

import ctypes
import logging
import platform
import sys
from ctypes import wintypes
from typing import Optional

WTS_CURRENT_SERVER_HANDLE = wintypes.HANDLE(0)
WTS_CURRENT_SESSION = 0xFFFFFFFF
WTS_SESSION_INFO_EX = 25
WTS_SESSIONSTATE_LOCK = 0
WTS_SESSIONSTATE_UNLOCK = 1


class WTSINFOEX_LEVEL1(ctypes.Structure):
    _fields_ = [
        ("SessionId", wintypes.ULONG),
        ("SessionState", ctypes.c_int),
        ("SessionFlags", wintypes.LONG),
        ("WinStationName", wintypes.WCHAR * 33),
        ("UserName", wintypes.WCHAR * 21),
        ("DomainName", wintypes.WCHAR * 18),
        ("LogonTime", ctypes.c_longlong),
        ("ConnectTime", ctypes.c_longlong),
        ("DisconnectTime", ctypes.c_longlong),
        ("LastInputTime", ctypes.c_longlong),
        ("CurrentTime", ctypes.c_longlong),
        ("IncomingBytes", wintypes.DWORD),
        ("OutgoingBytes", wintypes.DWORD),
        ("IncomingFrames", wintypes.DWORD),
        ("OutgoingFrames", wintypes.DWORD),
        ("IncomingCompressedBytes", wintypes.DWORD),
        ("OutgoingCompressedBytes", wintypes.DWORD),
    ]


class WTSINFOEX_LEVEL(ctypes.Union):
    _fields_ = [
        ("Level1", WTSINFOEX_LEVEL1),
    ]


class WTSINFOEX(ctypes.Structure):
    _fields_ = [
        ("Level", wintypes.DWORD),
        ("Data", WTSINFOEX_LEVEL),
    ]


if platform.system() == "Windows":
    wtsapi32 = ctypes.WinDLL("Wtsapi32.dll", use_last_error=True)
    wtsapi32.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wtsapi32.WTSFreeMemory.restype = None
else:
    wtsapi32 = None


def is_windows_7() -> bool:
    if platform.system() != "Windows":
        return False
    version = sys.getwindowsversion()
    return version.major == 6 and version.minor == 1


def get_windows_lock_state() -> Optional[bool]:
    """
    True=잠금, False=해제, None=확인 실패
    """
    if wtsapi32 is None:
        return None

    buffer = ctypes.c_void_p()
    bytes_returned = wintypes.DWORD(0)
    success = wtsapi32.WTSQuerySessionInformationW(
        WTS_CURRENT_SERVER_HANDLE,
        WTS_CURRENT_SESSION,
        WTS_SESSION_INFO_EX,
        ctypes.byref(buffer),
        ctypes.byref(bytes_returned),
    )
    if not success:
        logging.error(
            "WTS 상태 조회 실패, Windows 오류 코드=%s",
            ctypes.get_last_error(),
        )
        return None

    try:
        if not buffer.value:
            logging.error("WTS 상태 조회 결과의 버퍼가 비어 있음")
            return None

        info = ctypes.cast(buffer, ctypes.POINTER(WTSINFOEX)).contents
        if info.Level != 1:
            logging.error("지원하지 않는 WTSINFOEX 레벨=%s", info.Level)
            return None

        session_flag = int(info.Data.Level1.SessionFlags)
        if is_windows_7():
            session_flag = (
                WTS_SESSIONSTATE_UNLOCK
                if session_flag == WTS_SESSIONSTATE_LOCK
                else WTS_SESSIONSTATE_LOCK
            )

        if session_flag == WTS_SESSIONSTATE_LOCK:
            return True
        if session_flag == WTS_SESSIONSTATE_UNLOCK:
            return False

        logging.warning("알 수 없는 SessionFlags 값=%s", session_flag)
        return None
    except Exception:
        logging.exception("WTS 응답 처리 중 오류 발생")
        return None
    finally:
        if buffer.value:
            wtsapi32.WTSFreeMemory(buffer)
