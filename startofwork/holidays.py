"""공휴일 Open API 조회 및 캐시."""

from __future__ import annotations

import json
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Optional

import requests

from startofwork.constants import (
    HOLIDAY_API_RETRY_TIME,
    HOLIDAY_API_URL,
    HOLIDAY_SERVICE_KEY,
)
from startofwork.json_io import atomic_write_json
from startofwork.paths import HOLIDAY_CACHE_FILE

# 같은 날 반복 조회 시 디스크/API 생략
_memory_checked_date: Optional[str] = None
_memory_year_month: Optional[tuple[int, int]] = None
_memory_holidays: Optional[dict[str, str]] = None
_refresh_lock = threading.Lock()
_disk_cache: Optional[dict] = None
_disk_cache_mtime_ns: Optional[int] = None


def clear_holiday_memory_cache() -> None:
    global _memory_checked_date, _memory_year_month, _memory_holidays
    global _disk_cache, _disk_cache_mtime_ns
    _memory_checked_date = None
    _memory_year_month = None
    _memory_holidays = None
    _disk_cache = None
    _disk_cache_mtime_ns = None


def _file_mtime_ns() -> Optional[int]:
    try:
        return HOLIDAY_CACHE_FILE.stat().st_mtime_ns
    except OSError:
        return None


def load_holiday_cache() -> dict:
    global _disk_cache, _disk_cache_mtime_ns
    mtime_ns = _file_mtime_ns()
    if (
        _disk_cache is not None
        and mtime_ns is not None
        and mtime_ns == _disk_cache_mtime_ns
    ):
        return dict(_disk_cache)

    if not HOLIDAY_CACHE_FILE.exists():
        _disk_cache = {}
        _disk_cache_mtime_ns = mtime_ns
        return {}
    try:
        data = json.loads(HOLIDAY_CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        logging.exception("공휴일 캐시 읽기 실패")
        data = {}
    _disk_cache = dict(data)
    _disk_cache_mtime_ns = mtime_ns
    return dict(data)


def save_holiday_cache(payload: dict) -> None:
    global _disk_cache, _disk_cache_mtime_ns
    try:
        atomic_write_json(HOLIDAY_CACHE_FILE, payload)
        _disk_cache = dict(payload)
        _disk_cache_mtime_ns = _file_mtime_ns()
    except Exception:
        logging.exception("공휴일 캐시 저장 실패")
        _disk_cache = None
        _disk_cache_mtime_ns = None


def _remember_holidays(
    day: date, holidays_map: dict[str, str]
) -> dict[str, str]:
    global _memory_checked_date, _memory_year_month, _memory_holidays
    _memory_checked_date = day.isoformat()
    _memory_year_month = (day.year, day.month)
    _memory_holidays = dict(holidays_map)
    return _memory_holidays


def _parse_locdate(value: str) -> Optional[date]:
    text = (value or "").strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


def _local_tag(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def next_holiday_api_retry_at(now: Optional[datetime] = None) -> datetime:
    """다음 공휴일 API 재시도 시각(기본 당일/익일 08:00)."""
    current = now or datetime.now()
    today_retry = datetime.combine(current.date(), HOLIDAY_API_RETRY_TIME)
    if current < today_retry:
        return today_retry
    return datetime.combine(
        current.date() + timedelta(days=1), HOLIDAY_API_RETRY_TIME
    )


def _parse_retry_at(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def peek_holiday_api_retry_at() -> Optional[datetime]:
    return _parse_retry_at(load_holiday_cache().get("api_retry_at"))


def is_holiday_api_retry_due(now: Optional[datetime] = None) -> bool:
    """예약된 공휴일 API 재시도 시각이 지났으면 True."""
    current = now or datetime.now()
    retry_at = peek_holiday_api_retry_at()
    return retry_at is not None and retry_at <= current


def _persist_api_retry(cache: dict, *, now: datetime, error: BaseException) -> None:
    retry_at = next_holiday_api_retry_at(now)
    cache["api_error_at"] = now.isoformat(timespec="seconds")
    cache["api_error"] = f"{type(error).__name__}: {error}"[:300]
    cache["api_retry_at"] = retry_at.isoformat(timespec="seconds")
    save_holiday_cache(cache)
    logging.warning(
        "공휴일 Open API 조회 실패 — 기존 캐시 사용, %s에 재시도 예약 (%s)",
        retry_at.strftime("%Y-%m-%d %H:%M"),
        type(error).__name__,
    )


def _clear_api_retry_fields(cache: dict) -> dict:
    cache.pop("api_retry_at", None)
    cache.pop("api_error_at", None)
    cache.pop("api_error", None)
    return cache


def fetch_public_holidays(year: int, month: int) -> dict[str, str]:
    """Open API getRestDeInfo로 해당 연·월 공휴일 조회."""
    params = {
        "serviceKey": HOLIDAY_SERVICE_KEY,
        "pageNo": "1",
        "numOfRows": "100",
        "solYear": str(year),
        "solMonth": f"{month:02d}",
    }
    response = requests.get(HOLIDAY_API_URL, params=params, timeout=20)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    result_code = None
    result_msg = None
    items: list[ET.Element] = []

    for node in root.iter():
        tag = _local_tag(node.tag)
        if tag == "resultCode":
            result_code = (node.text or "").strip()
        elif tag == "resultMsg":
            result_msg = (node.text or "").strip()
        elif tag == "item":
            items.append(node)

    if result_code not in (None, "00", "0"):
        raise RuntimeError(
            f"공휴일 API 오류 code={result_code} msg={result_msg}"
        )

    holidays_map: dict[str, str] = {}
    for item in items:
        fields = {_local_tag(child.tag): (child.text or "").strip() for child in item}
        if fields.get("isHoliday", "Y").upper() != "Y":
            continue
        day = _parse_locdate(fields.get("locdate", ""))
        if day is None:
            continue
        name = fields.get("dateName") or "공휴일"
        holidays_map[day.isoformat()] = name

    return holidays_map


def normalize_holiday_map(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _holiday_maps_equal(left: dict[str, str], right: dict[str, str]) -> bool:
    return left == right


def _log_month_holidays(year: int, month: int, holidays_map: dict[str, str]) -> None:
    if not holidays_map:
        logging.info("공휴일 월간 기록: %04d-%02d — 공휴일 없음", year, month)
        return
    details = ", ".join(
        f"{day}={name}" for day, name in sorted(holidays_map.items())
    )
    logging.info(
        "공휴일 월간 기록: %04d-%02d count=%s [%s]",
        year,
        month,
        len(holidays_map),
        details,
    )


def refresh_holiday_cache_if_needed(
    today: Optional[date] = None,
    *,
    force: bool = False,
    cache_only: bool = False,
) -> dict[str, str]:
    """당월 공휴일 확인. 내용 변경 시에만 캐시 본문 갱신.

    cache_only=True 이면 API를 호출하지 않고 메모리/디스크 캐시만 사용한다.
    (메인 스레드에서 즉시 UI를 그릴 때 사용)
    """
    day = today or date.today()

    with _refresh_lock:
        if (
            not force
            and _memory_checked_date == day.isoformat()
            and _memory_year_month == (day.year, day.month)
            and _memory_holidays is not None
        ):
            return _memory_holidays

        cache = load_holiday_cache()
        checked = cache.get("checked_date")
        cached_year = cache.get("year")
        cached_month = cache.get("month")
        cached_holidays = normalize_holiday_map(cache.get("holidays"))

        if (
            not force
            and checked == day.isoformat()
            and cached_year == day.year
            and cached_month == day.month
            and not cache.get("api_retry_at")
        ):
            return _remember_holidays(day, cached_holidays)

        # UI 동기 경로: 당월 캐시가 있으면 checked_date가 어제가 되어도 네트워크 생략
        if cache_only and not force:
            if (
                _memory_year_month == (day.year, day.month)
                and _memory_holidays is not None
            ):
                return _memory_holidays
            if cached_year == day.year and cached_month == day.month:
                return _remember_holidays(day, cached_holidays)
            return _remember_holidays(day, {})

        # 새벽 점검 등으로 재시도가 미래로 잡혀 있으면 네트워크 생략
        retry_at = _parse_retry_at(cache.get("api_retry_at"))
        now = datetime.now()
        if retry_at is not None and retry_at > now:
            logging.info(
                "공휴일 API 재시도 대기(%s) — 캐시 사용",
                retry_at.strftime("%Y-%m-%d %H:%M"),
            )
            return _remember_holidays(day, cached_holidays)

        logging.info(
            "공휴일 Open API 확인%s: %04d-%02d",
            " (강제)" if force else "",
            day.year,
            day.month,
        )
        try:
            fetched = normalize_holiday_map(
                fetch_public_holidays(day.year, day.month)
            )
            same_month = cached_year == day.year and cached_month == day.month
            changed = (not same_month) or (
                not _holiday_maps_equal(cached_holidays, fetched)
            )
            checked_at = datetime.now().isoformat(timespec="seconds")

            if changed:
                if same_month:
                    logging.info(
                        "공휴일 변경 감지: 이전=%s → 신규=%s",
                        sorted(cached_holidays.items()),
                        sorted(fetched.items()),
                    )
                else:
                    logging.info(
                        "공휴일 월 변경 또는 최초 기록: %04d-%02d",
                        day.year,
                        day.month,
                    )
                payload = {
                    "checked_date": day.isoformat(),
                    "checked_at": checked_at,
                    "year": day.year,
                    "month": day.month,
                    "updated_at": checked_at,
                    "holidays": fetched,
                    "source": HOLIDAY_API_URL,
                }
                _clear_api_retry_fields(payload)
                save_holiday_cache(payload)
                _log_month_holidays(day.year, day.month, fetched)
                return _remember_holidays(day, fetched)

            cache["checked_date"] = day.isoformat()
            cache["checked_at"] = checked_at
            cache["year"] = day.year
            cache["month"] = day.month
            cache["holidays"] = cached_holidays
            cache["source"] = HOLIDAY_API_URL
            _clear_api_retry_fields(cache)
            save_holiday_cache(cache)
            logging.info(
                "공휴일 변경 없음 — 기존 월간 기록 유지: %04d-%02d count=%s",
                day.year,
                day.month,
                len(cached_holidays),
            )
            return _remember_holidays(day, cached_holidays)

        except (requests.RequestException, ET.ParseError, RuntimeError, OSError) as exc:
            logging.exception("공휴일 Open API 조회 실패 — 기존 캐시 사용")
            _persist_api_retry(cache, now=datetime.now(), error=exc)
            return _remember_holidays(day, cached_holidays)


def get_today_holiday_name(
    today: Optional[date] = None,
    *,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> Optional[str]:
    day = today or date.today()
    holidays_map = refresh_holiday_cache_if_needed(
        day, force=force_refresh, cache_only=cache_only
    )
    return holidays_map.get(day.isoformat())


def get_non_workday_reason(
    target: Optional[date] = None,
    *,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> Optional[str]:
    """근무일이 아니면 사유 문자열 반환 (토·일·공휴일)."""
    day = target or date.today()
    # 주말은 공휴일 맵 없이도 판정 가능. 월간 캐시 갱신은 강제/비주말 경로에서.
    if day.weekday() == 5:
        if force_refresh and not cache_only:
            refresh_holiday_cache_if_needed(day, force=True)
        return "토요일"
    if day.weekday() == 6:
        if force_refresh and not cache_only:
            refresh_holiday_cache_if_needed(day, force=True)
        return "일요일"

    holidays_map = refresh_holiday_cache_if_needed(
        day, force=force_refresh, cache_only=cache_only
    )
    holiday_name = holidays_map.get(day.isoformat())
    if holiday_name:
        return f"공휴일({holiday_name})"
    return None
