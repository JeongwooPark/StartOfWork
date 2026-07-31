"""config.json 로드/저장 및 로그인·업무시간·자동퇴근 설정."""

from __future__ import annotations

import json
import logging
from datetime import time as dt_time
from typing import Optional

from startofwork.constants import (
    DEFAULT_ACTIVE_END_TIME,
    DEFAULT_ACTIVE_START_TIME,
    DEFAULT_ATTENDANCE_URL,
    DEFAULT_AUTO_CHECKOUT_TIME,
)
from startofwork.credentials import (
    credential_target_for_url,
    get_password,
    set_password,
)
from startofwork.json_io import atomic_write_json, backup_corrupt_file
from startofwork.paths import CONFIG_FILE

_DEFAULT_CONFIG = {
    "attendance_url": "",
    "username": "",
    "credential_target": "",
    "active_start_time": "08:30",
    "active_end_time": "18:00",
    "auto_checkout_enabled": False,
    "auto_checkout_time": "18:00",
    "update_check_enabled": True,
}

_config_cache: Optional[dict] = None
_config_mtime_ns: Optional[int] = None
_active_hours_cache: Optional[tuple[dt_time, dt_time]] = None
_checkout_cache: Optional[tuple[bool, dt_time]] = None
_update_check_cache: Optional[bool] = None
_config_bootstrapped = False


def clear_config_cache() -> None:
    """테스트/강제 재로드용."""
    global _config_cache, _config_mtime_ns, _active_hours_cache, _checkout_cache
    global _update_check_cache, _config_bootstrapped
    _config_cache = None
    _config_mtime_ns = None
    _active_hours_cache = None
    _checkout_cache = None
    _update_check_cache = None
    _config_bootstrapped = False


def normalize_credential(value: object) -> str:
    return str(value or "").strip()


def normalize_attendance_url(value: object) -> str:
    return str(value or "").strip()


def is_missing_attendance_url(url: str) -> bool:
    text = normalize_attendance_url(url)
    if not text:
        return True
    lower = text.lower()
    return not (lower.startswith("http://") or lower.startswith("https://"))


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
    global _active_hours_cache, _checkout_cache, _update_check_cache
    _active_hours_cache = None
    _checkout_cache = None
    _update_check_cache = None


def _config_cache_fresh(mtime: Optional[int]) -> bool:
    return (
        _config_cache is not None
        and mtime is not None
        and mtime == _config_mtime_ns
    )


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
    # 평문 비밀번호는 config에 남기지 않는다
    payload = dict(data)
    payload.pop("password", None)
    atomic_write_json(CONFIG_FILE, payload)
    _config_cache = dict(payload)
    _config_mtime_ns = _file_mtime_ns()
    _invalidate_derived_caches()


def _resolve_credential_target(data: dict) -> str:
    existing = normalize_credential(data.get("credential_target", ""))
    if existing:
        return existing
    url = normalize_attendance_url(data.get("attendance_url", ""))
    if is_missing_attendance_url(url):
        return ""
    return credential_target_for_url(url)


def _migrate_plaintext_password(data: dict) -> bool:
    """config.json 평문 password → keyring 이전. 성공 시 True(파일 변경 필요)."""
    plaintext = str(data.get("password", "")).strip()
    if not plaintext:
        if "password" in data:
            del data["password"]
            return True
        return False

    username = normalize_credential(data.get("username", ""))
    target = _resolve_credential_target(data)
    if not target:
        # URL이 아직 없으면 username 기반으로 임시 target
        if username:
            target = f"StartOfWork:{username}"
        else:
            logging.warning(
                "평문 password 마이그레이션 보류 — username/URL 없음"
            )
            return False

    try:
        set_password(target, plaintext)
    except Exception:
        logging.exception(
            "평문 password → Credential Manager 이전 실패 — 설정 GUI 필요"
        )
        return False

    data["credential_target"] = target
    data.pop("password", None)
    logging.info(
        "평문 password를 Credential Manager로 이전: target=%s", target
    )
    return True


def ensure_app_config() -> dict:
    global _config_bootstrapped
    try:
        data = load_app_config()
    except FileNotFoundError:
        data = dict(_DEFAULT_CONFIG)
        save_app_config(data)
        _config_bootstrapped = True
        return data
    except Exception:
        logging.exception(
            "설정 파일 로드 실패 — 손상본 백업 후 기본값으로 재생성"
        )
        backup_corrupt_file(CONFIG_FILE)
        clear_config_cache()
        data = dict(_DEFAULT_CONFIG)
        save_app_config(data)
        _config_bootstrapped = True
        return data

    changed = False
    for key, value in _DEFAULT_CONFIG.items():
        if key in ("username", "credential_target", "attendance_url"):
            continue
        if key not in data:
            data[key] = value
            changed = True

    # 1.1.3 이하: attendance_url 키 없음 + 계정 있음 → 기본 URL로 마이그레이션
    if "attendance_url" not in data:
        username = normalize_credential(data.get("username", ""))
        plaintext = str(data.get("password", "")).strip()
        target = normalize_credential(data.get("credential_target", ""))
        stored_pw = get_password(target) if target else None
        has_creds = not is_missing_credentials(
            username, plaintext or (stored_pw or "")
        )
        if has_creds:
            data["attendance_url"] = DEFAULT_ATTENDANCE_URL
        else:
            data["attendance_url"] = ""
        changed = True

    if not normalize_credential(data.get("credential_target", "")):
        url = normalize_attendance_url(data.get("attendance_url", ""))
        if not is_missing_attendance_url(url):
            data["credential_target"] = credential_target_for_url(url)
            changed = True

    if _migrate_plaintext_password(data):
        changed = True

    if changed:
        try:
            save_app_config(data)
        except Exception:
            logging.exception("설정 기본값 보강 저장 실패")
    _config_bootstrapped = True
    return data


def load_attendance_url() -> str:
    data = load_app_config()
    url = normalize_attendance_url(data.get("attendance_url", ""))
    if is_missing_attendance_url(url):
        raise ValueError(
            f"설정 파일에 attendance_url이 필요합니다: {CONFIG_FILE}"
        )
    return url


def has_attendance_url() -> bool:
    try:
        load_attendance_url()
        return True
    except Exception:
        return False


def _password_from_config_data(data: dict) -> str:
    target = _resolve_credential_target(data)
    if not target:
        return ""
    return get_password(target) or ""


def load_login_credentials() -> tuple[str, str]:
    data = ensure_app_config()
    username = normalize_credential(data.get("username", ""))
    password = _password_from_config_data(data)
    if is_missing_credentials(username, password):
        raise ValueError(
            f"설정 파일에 username과 Credential Manager 비밀번호가 필요합니다: "
            f"{CONFIG_FILE}"
        )
    return username, password


def has_login_credentials() -> bool:
    try:
        load_login_credentials()
        return True
    except Exception:
        return False


def has_app_setup() -> bool:
    """근태 URL + 로그인 계정이 모두 있으면 True.

    최초 1회만 ensure_app_config로 구버전 보강 후, 이후는 단일 config 조회로 판정한다.
    """
    if _config_bootstrapped:
        try:
            data = load_app_config()
        except Exception:
            data = ensure_app_config()
    else:
        data = ensure_app_config()

    url = normalize_attendance_url(data.get("attendance_url", ""))
    username = normalize_credential(data.get("username", ""))
    password = _password_from_config_data(data)
    return not is_missing_attendance_url(url) and not is_missing_credentials(
        username, password
    )


def _store_credentials(
    data: dict,
    *,
    username: str,
    password: str,
    attendance_url: Optional[str] = None,
) -> dict:
    if attendance_url is not None:
        data["attendance_url"] = attendance_url
    url = normalize_attendance_url(data.get("attendance_url", ""))
    if is_missing_attendance_url(url):
        target = f"StartOfWork:{username}"
    else:
        target = credential_target_for_url(url)
    set_password(target, password)
    data["username"] = username
    data["credential_target"] = target
    data.pop("password", None)
    return data


def save_login_credentials(username: str, password: str) -> None:
    username = normalize_credential(username)
    password = str(password or "").strip()
    if is_missing_credentials(username, password):
        raise ValueError("아이디와 비밀번호를 입력하세요")

    data = ensure_app_config()
    data = _store_credentials(data, username=username, password=password)
    for key, value in _DEFAULT_CONFIG.items():
        if key in ("username", "credential_target"):
            continue
        data.setdefault(key, value)
    save_app_config(data)
    logging.info("로그인 설정 저장 완료: user=%s file=%s", username, CONFIG_FILE)


def save_app_setup(attendance_url: str, username: str, password: str) -> None:
    """최초 설정: 근태 URL + 계정을 검증 후 한 번에 저장."""
    attendance_url = normalize_attendance_url(attendance_url)
    username = normalize_credential(username)
    password = str(password or "").strip()
    if is_missing_attendance_url(attendance_url):
        raise ValueError("근태 페이지 URL을 http(s):// 형식으로 입력하세요")
    if is_missing_credentials(username, password):
        raise ValueError("아이디와 비밀번호를 입력하세요")

    data = ensure_app_config()
    data = _store_credentials(
        data,
        username=username,
        password=password,
        attendance_url=attendance_url,
    )
    for key, value in _DEFAULT_CONFIG.items():
        if key in ("username", "credential_target", "attendance_url"):
            continue
        data.setdefault(key, value)
    save_app_config(data)
    logging.info(
        "앱 설정 저장 완료: user=%s url=%s file=%s",
        username,
        attendance_url,
        CONFIG_FILE,
    )


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
    mtime = _file_mtime_ns()
    if _active_hours_cache is not None and _config_cache_fresh(mtime):
        return _active_hours_cache

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
    mtime = _file_mtime_ns()
    if _checkout_cache is not None and _config_cache_fresh(mtime):
        return _checkout_cache

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


def load_update_check_enabled() -> bool:
    global _update_check_cache
    mtime = _file_mtime_ns()
    if _update_check_cache is not None and _config_cache_fresh(mtime):
        return _update_check_cache

    try:
        data = load_app_config()
    except Exception:
        logging.exception("업데이트 설정 로드 실패 — 기본값 사용")
        return True

    _update_check_cache = bool(data.get("update_check_enabled", True))
    return _update_check_cache


def save_update_check_enabled(enabled: bool) -> None:
    global _update_check_cache
    try:
        data = ensure_app_config()
        data["update_check_enabled"] = bool(enabled)
        save_app_config(data)
        _update_check_cache = bool(enabled)
        logging.info("업데이트 확인 설정 저장: enabled=%s", bool(enabled))
    except Exception:
        logging.exception("업데이트 확인 설정 저장 실패")
