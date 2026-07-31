"""출근/퇴근·브라우저 실행 여부 판단 규칙."""

from __future__ import annotations

import logging
from datetime import date, datetime, time as dt_time
from typing import Optional

from startofwork.attendance_state import (
    load_last_check_in_date,
    load_last_check_out_date,
)
from startofwork.config import load_active_hours, load_auto_checkout_settings
from startofwork.constants import DEFAULT_AUTO_CHECKOUT_TIME
from startofwork.holidays import get_non_workday_reason


def is_within_active_hours(
    now: Optional[datetime] = None,
    *,
    hours: Optional[tuple[dt_time, dt_time]] = None,
) -> bool:
    current = (now or datetime.now()).time()
    start, end = hours if hours is not None else load_active_hours()
    return start <= current <= end


def should_open_browser(now: Optional[datetime] = None) -> tuple[bool, str]:
    """웹창을 띄워도 되는지 사전 검사."""
    current = now or datetime.now()
    start, end = load_active_hours()
    if not (start <= current.time() <= end):
        return False, "활성 시간대 외"

    reason = get_non_workday_reason(current.date())
    if reason is not None:
        return False, reason

    if load_last_check_in_date() == current.date():
        return False, "오늘 출근체크 완료"

    return True, "근무일"


def should_attempt_check_in(today: Optional[date] = None) -> bool:
    day = today or date.today()
    reason = get_non_workday_reason(day)
    if reason is not None:
        logging.info("근무일 아님(%s) — 출근하기 생략", reason)
        return False

    last = load_last_check_in_date()
    if last == day:
        logging.info(
            "오늘(%s) 이미 출근 처리됨 — 출근하기 생략",
            day.isoformat(),
        )
        return False

    if last is not None:
        logging.info(
            "이전 출근일=%s, 오늘=%s — 출근하기 진행",
            last.isoformat(),
            day.isoformat(),
        )
    else:
        logging.info("출근 기록 없음 — 출근하기 진행")
    return True


def should_attempt_check_out(
    today: Optional[date] = None,
    *,
    checkout_time: Optional[dt_time] = None,
    now: Optional[datetime] = None,
) -> bool:
    current = now or datetime.now()
    day = today or current.date()
    if checkout_time is None:
        _, checkout_time = load_auto_checkout_settings()
        checkout_time = checkout_time or DEFAULT_AUTO_CHECKOUT_TIME

    # 퇴근 시각 전이면 매초 검사해도 조용히 스킵 (로그 스팸 방지)
    if current.time() < checkout_time:
        return False

    reason = get_non_workday_reason(day)
    if reason is not None:
        logging.debug("근무일 아님(%s) — 퇴근하기 생략", reason)
        return False

    # 로컬 출근 기록은 필수 아님 — 서버 peek에서 최종 판정
    if load_last_check_out_date() == day:
        logging.debug("오늘(%s) 이미 퇴근 처리됨 — 퇴근하기 생략", day.isoformat())
        return False

    logging.info(
        "자동 퇴근 조건 충족 — date=%s time>=%s",
        day.isoformat(),
        checkout_time.strftime("%H:%M"),
    )
    return True
