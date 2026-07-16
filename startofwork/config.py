"""config.json 로드/저장 및 로그인·업무시간·자동퇴근 설정."""

from __future__ import annotations

import json
import logging
from datetime import time as dt_time
from typing import Optional

from startofwork.constants import (
    DEFAULT_ACTIVE_END_TIME,
    DEFAULT_ACTIVE_START_TIME,
    DEFAULT_AUTO_CHECKOUT_TIME,
)
from startofwork.paths import CONFIG_FILE

_DEFAULT_CONFIG = {
    "username": "",
    "password": "",
    "active_start_time": "08:30",
    "active_end_time": "18:00",
    "auto_checkout_enabled": False,
    "auto_checkout_time": "18:00",
}

_config_cache: Optional[dict] = None
_config_mtime_ns: Optional[int] = None
_active_hours_cache: Optional[tuple[dt_time, dt_time]] = None
_checkout_cache: Optional[tuple[bool, dt_time]] = None


def clear_config_cache() -> None:
    """테스트/강제 재로드용."""
    global _config_cache, _config_mtime_ns, _active_hours_cache, _checkout_cache
    _config_cache = None
    _config_mtime_ns = None
    _active_hours_cache = None
    _checkout_cache = None


def normalize_credential(value: object) -> str:
    return str(value or "").strip()


def is_missing_credentials(username: str, password: str) -> bool:
    if not username or not password:
        return True
    if username == "아이디" and password == "비밀번호":
        return True
    return False


def _file_mtime_ns() -> Optional[int]:
    try:
        return CONFIG_FILE.stat().st_mtime_ns
    except OSError:
        return None


def _invalidate_derived_caches() -> None:
    global _active_hours_cache, _checkout_cache
    _active_hours_cache = None
    _checkout_cache = None


def load_app_config() -> dict:
    global _config_cache, _config_mtime_ns

    mtime = _file_mtime_ns()
    if (
        _config_cache is not None
        and mtime is not None
        and mtime == _config_mtime_ns
    ):
        return dict(_config_cache)

    if not CONFIG_FILE.is_file():
        raise FileNotFoundError(f"설정 파일이 없습니다: {CONFIG_FILE}")
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"설정 파일 JSON 형식 오류: {CONFIG_FILE}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"설정 파일 형식이 올바르지 않습니다: {CONFIG_FILE}")

    _config_cache = dict(data)
    _config_mtime_ns = mtime
    _invalidate_derived_caches()
    return dict(data)


def save_app_config(data: dict) -> None:
    global _config_cache, _config_mtime_ns
    CONFIG_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _config_cache = dict(data)
    _config_mtime_ns = _file_mtime_ns()
    _invalidate_derived_caches()


def ensure_app_config() -> dict:
    try:
        data = load_app_config()
    except FileNotFoundError:
        data = dict(_DEFAULT_CONFIG)
        save_app_config(data)
        return data
    except Exception:
        logging.exception("설정 파일 로드 실패 — 기본값으로 재생성")
        data = dict(_DEFAULT_CONFIG)
        save_app_config(data)
        return data

    changed = False
    for key, value in _DEFAULT_CONFIG.items():
        if key in ("username", "password"):
            continue
        if key not in data:
            data[key] = value
            changed = True
    if changed:
        try:
            save_app_config(data)
        except Exception:
            logging.exception("설정 기본값 보강 저장 실패")
    return data


def load_login_credentials() -> tuple[str, str]:
    data = load_app_config()
    username = normalize_credential(data.get("username", ""))
    password = str(data.get("password", "")).strip()
    if is_missing_credentials(username, password):
        raise ValueError(
            f"설정 파일에 username/password가 필요합니다: {CONFIG_FILE}"
        )
    return username, password


def has_login_credentials() -> bool:
    try:
        load_login_credentials()
        return True
    except Exception:
        return False


def save_login_credentials(username: str, password: str) -> None:
    username = normalize_credential(username)
    password = str(password or "").strip()
    if is_missing_credentials(username, password):
        raise ValueError("아이디와 비밀번호를 입력하세요")

    data = ensure_app_config()
    data["username"] = username
    data["password"] = password
    for key, value in _DEFAULT_CONFIG.items():
        if key in ("username", "password"):
            continue
        data.setdefault(key, value)
    save_app_config(data)
    logging.info("로그인 설정 저장 완료: user=%s file=%s", username, CONFIG_FILE)


def parse_hhmm(
    value: str, default: dt_time = DEFAULT_AUTO_CHECKOUT_TIME
) -> dt_time:
    text = (value or "").strip()
    try:
        hour_s, minute_s = text.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("range")
        return dt_time(hour, minute)
    except Exception:
        return default


def _normalize_active_range(
    start: dt_time, end: dt_time
) -> tuple[dt_time, dt_time]:
    if start <= end:
        return start, end
    logging.warning(
        "업무시간 설정이 올바르지 않음(%s~%s) — 기본값 사용",
        start.strftime("%H:%M"),
        end.strftime("%H:%M"),
    )
    return DEFAULT_ACTIVE_START_TIME, DEFAULT_ACTIVE_END_TIME


def load_active_hours() -> tuple[dt_time, dt_time]:
    global _active_hours_cache
    # mtime 변경 시 load_app_config가 derived 캐시를 비움
    try:
        data = load_app_config()
    except Exception:
        logging.exception("업무시간 설정 로드 실패 — 기본값 사용")
        return DEFAULT_ACTIVE_START_TIME, DEFAULT_ACTIVE_END_TIME

    if _active_hours_cache is not None:
        return _active_hours_cache

    start = parse_hhmm(
        str(data.get("active_start_time", "08:30")),
        DEFAULT_ACTIVE_START_TIME,
    )
    end = parse_hhmm(
        str(data.get("active_end_time", "18:00")),
        DEFAULT_ACTIVE_END_TIME,
    )
    _active_hours_cache = _normalize_active_range(start, end)
    return _active_hours_cache


def save_active_hours(start: dt_time, end: dt_time) -> None:
    global _active_hours_cache
    start, end = _normalize_active_range(start, end)
    if _active_hours_cache is None:
        load_active_hours()
    if _active_hours_cache == (start, end):
        return
    try:
        data = ensure_app_config()
        data["active_start_time"] = start.strftime("%H:%M")
        data["active_end_time"] = end.strftime("%H:%M")
        save_app_config(data)
        _active_hours_cache = (start, end)
        logging.info(
            "업무시간 설정 저장: %s~%s",
            data["active_start_time"],
            data["active_end_time"],
        )
    except Exception:
        logging.exception("업무시간 설정 저장 실패")


def load_auto_checkout_settings() -> tuple[bool, dt_time]:
    global _checkout_cache
    try:
        data = load_app_config()
    except Exception:
        logging.exception("자동 퇴근 설정 로드 실패 — 기본값 사용")
        return False, DEFAULT_AUTO_CHECKOUT_TIME

    if _checkout_cache is not None:
        return _checkout_cache

    enabled = bool(data.get("auto_checkout_enabled", False))
    checkout_time = parse_hhmm(
        str(data.get("auto_checkout_time", "18:00")),
        DEFAULT_AUTO_CHECKOUT_TIME,
    )
    _checkout_cache = (enabled, checkout_time)
    return _checkout_cache


def save_auto_checkout_settings(enabled: bool, checkout_time: dt_time) -> None:
    global _checkout_cache
    enabled = bool(enabled)
    if _checkout_cache is None:
        load_auto_checkout_settings()
    if _checkout_cache == (enabled, checkout_time):
        return
    try:
        data = ensure_app_config()
        data["auto_checkout_enabled"] = enabled
        data["auto_checkout_time"] = checkout_time.strftime("%H:%M")
        save_app_config(data)
        _checkout_cache = (enabled, checkout_time)
        logging.info(
            "자동 퇴근 설정 저장: enabled=%s time=%s",
            enabled,
            data["auto_checkout_time"],
        )
    except Exception:
        logging.exception("자동 퇴근 설정 저장 실패")
