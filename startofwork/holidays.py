"""공휴일 Open API 조회 및 캐시."""

from __future__ import annotations

import json
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Optional

import requests

from startofwork.constants import HOLIDAY_API_URL, HOLIDAY_SERVICE_KEY
from startofwork.paths import HOLIDAY_CACHE_FILE

# 같은 날 반복 조회 시 디스크/API 생략
_memory_checked_date: Optional[str] = None
_memory_year_month: Optional[tuple[int, int]] = None
_memory_holidays: Optional[dict[str, str]] = None
_refresh_lock = threading.Lock()


def clear_holiday_memory_cache() -> None:
    global _memory_checked_date, _memory_year_month, _memory_holidays
    _memory_checked_date = None
    _memory_year_month = None
    _memory_holidays = None


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


def load_holiday_cache() -> dict:
    if not HOLIDAY_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(HOLIDAY_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("공휴일 캐시 읽기 실패")
        return {}


def save_holiday_cache(payload: dict) -> None:
    try:
        HOLIDAY_CACHE_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        logging.exception("공휴일 캐시 저장 실패")


def normalize_holiday_map(raw) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _holiday_maps_equal(left: dict[str, str], right: dict[str, str]) -> bool:
    return sorted(left.items()) == sorted(right.items())


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
                save_holiday_cache(payload)
                _log_month_holidays(day.year, day.month, fetched)
                return _remember_holidays(day, fetched)

            cache["checked_date"] = day.isoformat()
            cache["checked_at"] = checked_at
            cache["year"] = day.year
            cache["month"] = day.month
            cache["holidays"] = cached_holidays
            cache["source"] = HOLIDAY_API_URL
            save_holiday_cache(cache)
            logging.info(
                "공휴일 변경 없음 — 기존 월간 기록 유지: %04d-%02d count=%s",
                day.year,
                day.month,
                len(cached_holidays),
            )
            return _remember_holidays(day, cached_holidays)

        except (requests.RequestException, ET.ParseError, RuntimeError, OSError):
            logging.exception("공휴일 Open API 조회 실패 — 기존 캐시 사용")
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
