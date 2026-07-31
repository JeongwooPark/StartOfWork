"""출근/퇴근 상태 파일 및 표시 문구."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from startofwork.holidays import get_non_workday_reason
from startofwork.json_io import atomic_write_json
from startofwork.paths import CHECK_IN_STATE_FILE

ActionKind = Literal["check_in", "check_out"]
ResultKind = Literal["success", "failed", "unknown"]
ErrorKind = Literal[
    "network",
    "button_not_found",
    "auth",
    "verify_failed",
    "verify_unknown",
    "other",
]

# 네트워크/타임아웃: 2 → 5 → 10분, 최대 3회
_NETWORK_RETRY_MINUTES = (2, 5, 10)
_BUTTON_NOT_FOUND_RETRY_MINUTES = 10
_BUTTON_NOT_FOUND_MAX_EXTRA = 1
_UNKNOWN_MAX_RETRY = 1

_state_cache: Optional[dict] = None
_state_mtime_ns: Optional[int] = None


def clear_check_in_state_cache() -> None:
    global _state_cache, _state_mtime_ns
    _state_cache = None
    _state_mtime_ns = None


def _file_mtime_ns() -> Optional[int]:
    try:
        return CHECK_IN_STATE_FILE.stat().st_mtime_ns
    except OSError:
        return None


def load_check_in_state() -> dict:
    global _state_cache, _state_mtime_ns

    mtime = _file_mtime_ns()
    if (
        _state_cache is not None
        and mtime is not None
        and mtime == _state_mtime_ns
    ):
        return dict(_state_cache)

    if not CHECK_IN_STATE_FILE.exists():
        _state_cache = {}
        _state_mtime_ns = None
        return {}
    try:
        data = json.loads(CHECK_IN_STATE_FILE.read_text(encoding="utf-8"))
        data = data if isinstance(data, dict) else {}
    except Exception:
        logging.exception("출근 상태 파일 읽기 실패")
        return {}

    _state_cache = dict(data)
    _state_mtime_ns = mtime
    return dict(data)


def peek_check_in_state() -> dict:
    """캐시된 상태 dict를 복사 없이 반환. 호출자가 변이하면 안 된다."""
    global _state_cache, _state_mtime_ns

    mtime = _file_mtime_ns()
    if (
        _state_cache is not None
        and mtime is not None
        and mtime == _state_mtime_ns
    ):
        return _state_cache

    return load_check_in_state()


def _write_check_in_state(payload: dict) -> None:
    global _state_cache, _state_mtime_ns
    atomic_write_json(CHECK_IN_STATE_FILE, payload)
    _state_cache = dict(payload)
    _state_mtime_ns = _file_mtime_ns()


def _parse_state_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_state_datetime(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def load_last_check_in_date() -> Optional[date]:
    return _parse_state_date(peek_check_in_state().get("last_check_in_date"))


def load_last_check_out_date() -> Optional[date]:
    return _parse_state_date(peek_check_in_state().get("last_check_out_date"))


def load_last_attendance_dates() -> tuple[Optional[date], Optional[date]]:
    """출근·퇴근 날짜를 한 번의 상태 조회로 반환."""
    state = peek_check_in_state()
    return (
        _parse_state_date(state.get("last_check_in_date")),
        _parse_state_date(state.get("last_check_out_date")),
    )


def format_check_in_time(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = str(raw).strip()
    try:
        if "T" in text:
            dt = datetime.fromisoformat(text)
        else:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%H:%M:%S")
    except ValueError:
        return text.replace("T", " ")


def _prefix(action: ActionKind) -> str:
    return "check_in" if action == "check_in" else "check_out"


def _retry_delay_minutes(
    error_kind: ErrorKind, retry_count: int
) -> Optional[int]:
    """다음 재시도까지 대기 분. None이면 재시도 중단."""
    if error_kind == "auth":
        return None
    if error_kind == "verify_unknown":
        if retry_count >= _UNKNOWN_MAX_RETRY:
            return None
        return _NETWORK_RETRY_MINUTES[0]
    if error_kind == "button_not_found":
        if retry_count >= _BUTTON_NOT_FOUND_MAX_EXTRA:
            return None
        return _BUTTON_NOT_FOUND_RETRY_MINUTES
    if error_kind in ("network", "verify_failed", "other"):
        if retry_count >= len(_NETWORK_RETRY_MINUTES):
            return None
        return _NETWORK_RETRY_MINUTES[retry_count]
    return None


def record_attempt(
    action: ActionKind,
    *,
    now: Optional[datetime] = None,
) -> None:
    current = now or datetime.now()
    prefix = _prefix(action)
    payload = load_check_in_state()
    payload[f"last_{prefix}_attempt"] = current.isoformat(timespec="seconds")
    try:
        _write_check_in_state(payload)
    except Exception:
        logging.exception("%s 시도 시각 저장 실패", action)


def record_success(
    action: ActionKind,
    day: date,
    *,
    now: Optional[datetime] = None,
) -> bool:
    current = now or datetime.now()
    prefix = _prefix(action)
    payload = load_check_in_state()
    payload[f"last_{prefix}_date"] = day.isoformat()
    payload[f"last_{prefix}_at"] = current.isoformat(timespec="seconds")
    payload[f"last_{prefix}_result"] = "success"
    payload[f"last_{prefix}_attempt"] = current.isoformat(timespec="seconds")
    payload[f"last_{prefix}_error"] = ""
    payload[f"{prefix}_retry_count"] = 0
    payload.pop(f"next_{prefix}_retry_at", None)
    try:
        _write_check_in_state(payload)
        logging.info(
            "%s 성공 기록: date=%s file=%s",
            action,
            day.isoformat(),
            CHECK_IN_STATE_FILE.name,
        )
        return True
    except Exception:
        logging.exception("%s 성공 상태 저장 실패", action)
        return False


def record_failure(
    action: ActionKind,
    error_kind: ErrorKind,
    message: str,
    *,
    result: ResultKind = "failed",
    now: Optional[datetime] = None,
) -> None:
    current = now or datetime.now()
    prefix = _prefix(action)
    payload = load_check_in_state()
    prev_attempt = _parse_state_datetime(payload.get(f"last_{prefix}_attempt"))
    # 시도일이 오늘이 아니면 카운트 리셋
    if prev_attempt is None or prev_attempt.date() != current.date():
        retry_count = 0
    else:
        retry_count = int(payload.get(f"{prefix}_retry_count") or 0)

    payload[f"last_{prefix}_result"] = result
    payload[f"last_{prefix}_attempt"] = current.isoformat(timespec="seconds")
    payload[f"last_{prefix}_error"] = f"{error_kind}: {message}"[:500]

    delay = _retry_delay_minutes(error_kind, retry_count)
    if delay is None:
        payload[f"{prefix}_retry_count"] = retry_count
        payload.pop(f"next_{prefix}_retry_at", None)
        logging.warning(
            "%s 실패 — 재시도 중단 (%s): %s", action, error_kind, message
        )
    else:
        next_count = retry_count + 1
        payload[f"{prefix}_retry_count"] = next_count
        next_at = current + timedelta(minutes=delay)
        payload[f"next_{prefix}_retry_at"] = next_at.isoformat(
            timespec="seconds"
        )
        logging.warning(
            "%s 실패 (%s) — %s분 후 재시도 (%s회): %s",
            action,
            error_kind,
            delay,
            next_count,
            message,
        )

    try:
        _write_check_in_state(payload)
    except Exception:
        logging.exception("%s 실패 상태 저장 실패", action)


def save_check_in_date(day: date) -> bool:
    return record_success("check_in", day)


def save_check_out_date(day: date) -> bool:
    return record_success("check_out", day)


def is_auth_failure_blocking(
    action: ActionKind, *, today: Optional[date] = None
) -> bool:
    day = today or date.today()
    state = peek_check_in_state()
    prefix = _prefix(action)
    if state.get(f"last_{prefix}_result") not in ("failed", "unknown"):
        return False
    attempt = _parse_state_datetime(state.get(f"last_{prefix}_attempt"))
    if attempt is None or attempt.date() != day:
        return False
    error = str(state.get(f"last_{prefix}_error") or "")
    return error.startswith("auth:")


def is_retry_due(
    action: ActionKind, *, now: Optional[datetime] = None
) -> bool:
    """실패 후 재시도 시각이 지났으면 True. 성공·미시도는 False."""
    current = now or datetime.now()
    state = peek_check_in_state()
    prefix = _prefix(action)
    result = state.get(f"last_{prefix}_result")
    if result not in ("failed", "unknown"):
        return False
    attempt = _parse_state_datetime(state.get(f"last_{prefix}_attempt"))
    if attempt is None or attempt.date() != current.date():
        return False
    if is_auth_failure_blocking(action, today=current.date()):
        return False
    next_at = _parse_state_datetime(state.get(f"next_{prefix}_retry_at"))
    if next_at is None:
        return False
    return current >= next_at


def is_attempt_allowed(
    action: ActionKind, *, now: Optional[datetime] = None
) -> bool:
    """당일 첫 시도이거나 재시도 시각이 지났으면 True."""
    current = now or datetime.now()
    state = peek_check_in_state()
    prefix = _prefix(action)
    result = state.get(f"last_{prefix}_result")
    attempt = _parse_state_datetime(state.get(f"last_{prefix}_attempt"))

    if result not in ("failed", "unknown"):
        return True
    if attempt is None or attempt.date() != current.date():
        return True
    if is_auth_failure_blocking(action, today=current.date()):
        return False
    next_at = _parse_state_datetime(state.get(f"next_{prefix}_retry_at"))
    if next_at is None:
        # 재시도 스케줄 없음 = 상한 도달 또는 auth
        return False
    return current >= next_at


def _non_workday_status_text(reason: str) -> str:
    if reason.startswith("공휴일"):
        return "공휴일로 체크하지 않음"
    return f"대상 아님 ({reason})"


def _status_from_state(
    state: dict,
    day: date,
    *,
    date_key: str,
    time_key: str,
    non_workday_reason: Optional[str],
) -> str:
    if non_workday_reason is not None:
        return _non_workday_status_text(non_workday_reason)

    last = _parse_state_date(state.get(date_key))
    if last == day:
        checked_time = format_check_in_time(state.get(time_key))
        if checked_time:
            return f"완료 ({checked_time})"
        return "완료"
    return "미완료"


def _status_text_for(
    *,
    today: Optional[date],
    date_key: str,
    time_key: str,
) -> str:
    day = today or date.today()
    reason = get_non_workday_reason(day, force_refresh=False)
    return _status_from_state(
        peek_check_in_state(),
        day,
        date_key=date_key,
        time_key=time_key,
        non_workday_reason=reason,
    )


def get_check_in_status_text(today: Optional[date] = None) -> str:
    return _status_text_for(
        today=today,
        date_key="last_check_in_date",
        time_key="last_check_in_at",
    )


def get_check_out_status_text(today: Optional[date] = None) -> str:
    return _status_text_for(
        today=today,
        date_key="last_check_out_date",
        time_key="last_check_out_at",
    )


def get_tray_status_text(today: Optional[date] = None) -> str:
    """트레이 툴팁용 요약: 퇴근 > 출근 > 미완료, 비근무일은 체크 생략 안내."""
    day = today or date.today()
    reason = get_non_workday_reason(day, force_refresh=False)
    if reason is not None:
        if reason.startswith("공휴일"):
            return "공휴일로 체크하지 않음"
        return f"{reason} — 체크하지 않음"

    state = peek_check_in_state()
    last_out = _parse_state_date(state.get("last_check_out_date"))
    if last_out == day:
        checked_time = format_check_in_time(state.get("last_check_out_at"))
        if checked_time:
            return f"퇴근체크: 완료 ({checked_time})"
        return "퇴근체크: 완료"

    last_in = _parse_state_date(state.get("last_check_in_date"))
    if last_in == day:
        checked_time = format_check_in_time(state.get("last_check_in_at"))
        if checked_time:
            return f"출근체크: 완료 ({checked_time})"
        return "출근체크: 완료"

    return "출근체크: 미완료"


def get_monitor_attendance_snapshot(
    today: Optional[date] = None,
) -> tuple[str, str, Optional[date], Optional[date], Optional[str]]:
    """모니터 1틱용: 상태 문구·날짜·비근무일 사유를 한 번에 조회."""
    day = today or date.today()
    reason = get_non_workday_reason(day, force_refresh=False)
    state = peek_check_in_state()
    last_in = _parse_state_date(state.get("last_check_in_date"))
    last_out = _parse_state_date(state.get("last_check_out_date"))
    check_in_text = _status_from_state(
        state,
        day,
        date_key="last_check_in_date",
        time_key="last_check_in_at",
        non_workday_reason=reason,
    )
    check_out_text = _status_from_state(
        state,
        day,
        date_key="last_check_out_date",
        time_key="last_check_out_at",
        non_workday_reason=reason,
    )
    return check_in_text, check_out_text, last_in, last_out, reason
