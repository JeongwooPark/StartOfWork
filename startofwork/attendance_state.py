"""출근/퇴근 상태 파일 및 표시 문구."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import Optional

from startofwork.holidays import get_non_workday_reason
from startofwork.paths import CHECK_IN_STATE_FILE

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


def _write_check_in_state(payload: dict) -> None:
    global _state_cache, _state_mtime_ns
    CHECK_IN_STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _state_cache = dict(payload)
    _state_mtime_ns = _file_mtime_ns()


def _parse_state_date(value) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def load_last_check_in_date() -> Optional[date]:
    return _parse_state_date(load_check_in_state().get("last_check_in_date"))


def load_last_check_out_date() -> Optional[date]:
    return _parse_state_date(load_check_in_state().get("last_check_out_date"))


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


def save_check_in_date(day: date) -> None:
    payload = load_check_in_state()
    payload["last_check_in_date"] = day.isoformat()
    payload["last_check_in_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _write_check_in_state(payload)
        logging.info(
            "출근 처리 기록 저장: date=%s file=%s",
            day.isoformat(),
            CHECK_IN_STATE_FILE.name,
        )
    except Exception:
        logging.exception("출근 상태 파일 저장 실패")


def save_check_out_date(day: date) -> None:
    payload = load_check_in_state()
    payload["last_check_out_date"] = day.isoformat()
    payload["last_check_out_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        _write_check_in_state(payload)
        logging.info(
            "퇴근 처리 기록 저장: date=%s file=%s",
            day.isoformat(),
            CHECK_IN_STATE_FILE.name,
        )
    except Exception:
        logging.exception("퇴근 상태 파일 저장 실패")


def _status_text_for(
    *,
    today: Optional[date],
    date_key: str,
    time_key: str,
) -> str:
    day = today or date.today()
    reason = get_non_workday_reason(day, force_refresh=False)
    if reason is not None:
        return f"대상 아님 ({reason})"

    state = load_check_in_state()
    last = _parse_state_date(state.get(date_key))
    if last == day:
        checked_time = format_check_in_time(state.get(time_key))
        if checked_time:
            return f"완료 ({checked_time})"
        return "완료"
    return "미완료"


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
