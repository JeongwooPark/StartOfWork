"""Chrome(Selenium) 기반 근태 로그인·출근·퇴근 자동화."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Literal, Optional

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
import selenium.webdriver.chrome.webdriver  # noqa: F401
import selenium.webdriver.chromium.webdriver  # noqa: F401
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from startofwork.attendance_state import (
    load_last_check_out_date,
    record_failure,
    save_check_in_date,
    save_check_out_date,
)
from startofwork.config import (
    has_app_setup,
    load_attendance_url,
    load_login_credentials,
)
from startofwork.constants import (
    ATTENDANCE_PAGE_WAIT_SEC,
    CHECK_IN_BUTTON_XPATH,
    CHECK_IN_RENDER_WAIT_SEC,
    CHECK_OUT_BUTTON_XPATH,
)
from startofwork.notifications import (
    notify_attendance_failure,
    notify_check_in_done,
    notify_check_out_done,
)
from startofwork.paths import CHROME_PROFILE_DIR
from startofwork.rules import should_attempt_check_in, should_open_browser

AttendanceUiState = Literal[
    "not_checked_in", "checked_in", "checked_out", "unknown"
]

VERIFY_WAIT_SEC = 15
VERIFY_REFRESH_WAIT_SEC = 8
CONFIRM_PROMPT_WAIT_SEC = 3.0

_active_drivers: list[webdriver.Chrome] = []
_attendance_lock = threading.Lock()
_checkout_job_running = False
_checkout_rearm_requested = False
_cached_chrome: Optional[Path] = None
_chrome_resolved = False

# text 기반 xpath 우선 — absolute 경로는 폴백
CHECK_IN_BUTTON_XPATHS = (
    "//button[.//span[contains(normalize-space(.), '출근하기')]]",
    "//button[contains(., '출근하기')]",
    CHECK_IN_BUTTON_XPATH,
    "/html/body/div[3]/div/main/div[1]/div[1]/div[3]/div[3]/button[1]",
)
CHECK_OUT_BUTTON_XPATHS = (
    "//button[.//span[contains(normalize-space(.), '퇴근하기')]]",
    "//button[contains(., '퇴근하기')]",
    CHECK_OUT_BUTTON_XPATH,
    "/html/body/div[3]/div/main/div[1]/div[1]/div[3]/div[3]/button[2]",
)
ATTENDANCE_ACTION_XPATHS = CHECK_IN_BUTTON_XPATHS + CHECK_OUT_BUTTON_XPATHS

# 출퇴근 클릭 직후 뜨는 확인 모달/레이어 (다우오피스 등)
# 페이지 전역 '확인'은 오클릭 위험이 있어 dialog/modal 내부만 대상으로 한다.
_DIALOG_ROOT = (
    "*[@role='dialog' or contains(@class,'modal') or contains(@class,'dialog') "
    "or contains(@class,'popup') or contains(@class,'layer') "
    "or contains(@class,'message-box') or contains(@class,'confirm')]"
)
CONFIRM_BUTTON_XPATHS = (
    f"//{_DIALOG_ROOT}//button[normalize-space(.)='확인' "
    f"or .//span[normalize-space(.)='확인']]",
    f"//{_DIALOG_ROOT}//button[normalize-space(.)='예' "
    f"or .//span[normalize-space(.)='예']]",
    f"//{_DIALOG_ROOT}//button[normalize-space(.)='OK' "
    f"or normalize-space(.)='Yes']",
    f"//{_DIALOG_ROOT}//*[@role='button'][normalize-space(.)='확인' "
    f"or normalize-space(.)='예']",
)

# 성공 UI: 버튼은 회색으로 남고, 출근/퇴근 시간에 HH:MM:SS가 채워짐
_TIME_TEXT_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3]):[0-5]\d:[0-5]\d(?!\d)")
CHECK_IN_TIME_LABEL = "출근 시간"
CHECK_OUT_TIME_LABEL = "퇴근 시간"


def is_checkout_job_running() -> bool:
    return _checkout_job_running


def request_checkout_rearm() -> None:
    """자동 퇴근이 서버 미출근 등으로 스킵된 경우 GUI 재시도를 허용."""
    global _checkout_rearm_requested
    _checkout_rearm_requested = True


def consume_checkout_rearm() -> bool:
    global _checkout_rearm_requested
    if not _checkout_rearm_requested:
        return False
    _checkout_rearm_requested = False
    return True


def find_chrome_executable() -> Optional[Path]:
    global _cached_chrome, _chrome_resolved
    if _chrome_resolved:
        return _cached_chrome

    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google"
        / "Chrome"
        / "Application"
        / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            _cached_chrome = candidate
            break
    _chrome_resolved = True
    return _cached_chrome


def create_chrome_options(chrome: Path) -> ChromeOptions:
    options = ChromeOptions()
    options.binary_location = str(chrome)
    CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={CHROME_PROFILE_DIR}")
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-notifications")
    options.add_argument("--mute-audio")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    return options


def _close_browser(driver: Optional[webdriver.Chrome]) -> None:
    if driver is None:
        return
    try:
        driver.quit()
        logging.info("Chrome 창 종료 완료")
    except Exception:
        logging.exception("Chrome 창 종료 실패")
    finally:
        if driver in _active_drivers:
            _active_drivers.remove(driver)


def _set_input_value(driver: webdriver.Chrome, element, value: str) -> None:
    element.click()
    element.send_keys(Keys.CONTROL, "a")
    element.send_keys(Keys.BACKSPACE)
    element.send_keys(value)
    driver.execute_script(
        """
        const el = arguments[0];
        const value = arguments[1];
        const nativeSetter = Object.getOwnPropertyDescriptor(
            window.HTMLInputElement.prototype, 'value'
        ).set;
        nativeSetter.call(el, value);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element,
        value,
    )


def _find_visible(driver: webdriver.Chrome, css: str):
    for element in driver.find_elements(By.CSS_SELECTOR, css):
        try:
            if element.is_displayed() and element.is_enabled():
                return element
        except Exception:
            continue
    return None


def _resolve_button(element):
    target = element
    if (element.tag_name or "").lower() != "button":
        try:
            target = element.find_element(By.XPATH, "./ancestor::button[1]")
        except Exception:
            target = element
    return target


def _is_effectively_enabled(element) -> bool:
    """HTML disabled / aria-disabled / disabled 클래스를 함께 본다."""
    try:
        if not element.is_enabled():
            return False
    except Exception:
        return False
    try:
        aria = (element.get_attribute("aria-disabled") or "").strip().lower()
        if aria in ("true", "1"):
            return False
    except Exception:
        pass
    try:
        cls = (element.get_attribute("class") or "").lower()
        if "is-disabled" in cls or "disabled" in cls.split():
            return False
    except Exception:
        pass
    return True


def find_button_by_xpaths(
    driver: webdriver.Chrome,
    xpaths: tuple[str, ...],
    *,
    require_enabled: bool = True,
):
    for xpath in xpaths:
        try:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if not element.is_displayed():
                        continue
                    target = _resolve_button(element)
                    if not target.is_displayed():
                        continue
                    if require_enabled and not _is_effectively_enabled(target):
                        continue
                    return target
                except Exception:
                    continue
        except Exception:
            continue
    return None


def wait_for_attendance_url(
    driver: webdriver.Chrome,
    attendance_url: Optional[str] = None,
) -> bool:
    try:
        target = (attendance_url or load_attendance_url()).strip()
    except Exception:
        logging.exception("근태 URL 로드 실패")
        return False

    current = driver.current_url or ""
    on_target = target.rstrip("/") in current.rstrip("/") or (
        "my-attendance-status" in current and "/login" not in current
    )
    if not on_target:
        logging.info("근태 화면으로 이동 중...")
        driver.get(target)
    try:
        WebDriverWait(driver, ATTENDANCE_PAGE_WAIT_SEC).until(
            lambda d: "/login" not in (d.current_url or "")
            and (
                target.rstrip("/") in (d.current_url or "").rstrip("/")
                or "my-attendance-status" in (d.current_url or "")
            )
        )
        return True
    except TimeoutException:
        logging.warning("근태 페이지 URL 전환 실패: %s", driver.current_url)
        return False


def _click_element(driver: webdriver.Chrome, target) -> None:
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});",
        target,
    )
    time.sleep(0.15)
    # SPA(Angular 등)에서는 JS click이 더 안정적인 경우가 많음
    try:
        driver.execute_script("arguments[0].click();", target)
    except Exception:
        target.click()


def _accept_native_alert(driver: webdriver.Chrome) -> bool:
    try:
        alert = driver.switch_to.alert
        text = (alert.text or "").strip()
        alert.accept()
        logging.info("네이티브 확인창 수락: %s", text or "(empty)")
        return True
    except Exception:
        return False


def _click_confirm_dialog_button(driver: webdriver.Chrome) -> bool:
    """출퇴근 클릭 후 뜨는 DOM 확인 모달의 확인/예 버튼을 클릭."""
    button = find_button_by_xpaths(driver, CONFIRM_BUTTON_XPATHS)
    if button is None:
        return False
    try:
        label = (button.text or "").strip() or "확인"
        _click_element(driver, button)
        logging.info("확인 모달 버튼 클릭: %s", label)
        return True
    except Exception:
        logging.exception("확인 모달 버튼 클릭 실패")
        return False


def _handle_post_click_prompts(
    driver: webdriver.Chrome,
    *,
    timeout_sec: float = CONFIRM_PROMPT_WAIT_SEC,
) -> bool:
    """클릭 직후 native alert / 확인 모달을 짧은 시간 동안 처리."""
    deadline = time.time() + timeout_sec
    handled = False
    while time.time() < deadline:
        if _accept_native_alert(driver):
            handled = True
            time.sleep(0.3)
            continue
        if _click_confirm_dialog_button(driver):
            handled = True
            time.sleep(0.4)
            continue
        if handled:
            break
        time.sleep(0.2)
    return handled


def _click_labeled_button(
    driver: webdriver.Chrome,
    *,
    xpaths: tuple[str, ...],
    label: str,
) -> bool:
    """버튼 클릭만 수행. 저장/알림은 호출측에서 검증 후 처리."""
    if not wait_for_attendance_url(driver):
        return False

    logging.info("근태 URL 도착 — %s 버튼 렌더 대기", label)
    button_wait = WebDriverWait(driver, CHECK_IN_RENDER_WAIT_SEC)
    try:
        target = button_wait.until(
            lambda d: find_button_by_xpaths(d, xpaths) or False
        )
    except TimeoutException:
        logging.error(
            "%s 버튼 대기 시간 초과(%s초): %s",
            label,
            CHECK_IN_RENDER_WAIT_SEC,
            driver.current_url,
        )
        return False

    try:
        _click_element(driver, target)
        logging.info("%s 버튼 클릭 완료", label)
        if _handle_post_click_prompts(driver):
            logging.info("%s 클릭 후 확인 팝업 처리 완료", label)
        else:
            time.sleep(0.5)
        return True
    except Exception:
        logging.exception("%s 버튼 클릭 실패", label)
        return False


def _normalize_ui_text(value: object) -> str:
    return " ".join(str(value or "").split())


def _extract_clock_time(text: str) -> Optional[str]:
    match = _TIME_TEXT_RE.search(_normalize_ui_text(text))
    return match.group(0) if match else None


def _extract_clock_time_after_label(text: str, label: str) -> Optional[str]:
    """라벨 뒤에 나온 첫 HH:MM:SS만 사용 (같은 박스의 다른 시각 오인 방지)."""
    normalized = _normalize_ui_text(text)
    if label not in normalized:
        return None
    after = normalized.split(label, 1)[1]
    for other in (CHECK_IN_TIME_LABEL, CHECK_OUT_TIME_LABEL):
        if other != label and other in after:
            after = after.split(other, 1)[0]
            break
    return _extract_clock_time(after)


def _find_labeled_clock_time(driver: webdriver.Chrome, label: str) -> Optional[str]:
    """'출근 시간'/'퇴근 시간' 라벨 옆 p.data 의 HH:MM:SS를 읽는다."""
    xpaths = (
        (
            f"//p[contains(@class,'tit') and normalize-space(.)='{label}']"
            f"/following-sibling::p[contains(@class,'data')][1]"
        ),
        (
            f"//*[contains(@class,'work_type')]"
            f"[.//*[contains(@class,'tit') and normalize-space(.)='{label}']]"
            f"//*[contains(@class,'data')][1]"
        ),
    )
    for xpath in xpaths:
        try:
            for element in driver.find_elements(By.XPATH, xpath):
                try:
                    if not element.is_displayed():
                        continue
                    text = _normalize_ui_text(element.text)
                    if not text or text in {"-", "–", "—"}:
                        return None
                    found = _extract_clock_time(text)
                    if found:
                        return found
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _read_clock_times(
    driver: webdriver.Chrome,
) -> tuple[Optional[str], Optional[str]]:
    """근무시간 박스에서 출근/퇴근 시각을 한 번에 읽는다."""
    in_time: Optional[str] = None
    out_time: Optional[str] = None
    found_in_label = False
    found_out_label = False
    try:
        blocks = driver.find_elements(By.CSS_SELECTOR, "div.work_type")
    except Exception:
        blocks = []

    for block in blocks:
        try:
            if not block.is_displayed():
                continue
            tit = block.find_element(By.CSS_SELECTOR, "p.tit, .tit")
            data = block.find_element(By.CSS_SELECTOR, "p.data, .data")
            label = _normalize_ui_text(tit.text)
            raw = _normalize_ui_text(data.text)
            value = None if (not raw or raw in {"-", "–", "—"}) else _extract_clock_time(raw)
            if label == CHECK_IN_TIME_LABEL:
                found_in_label = True
                in_time = value
            elif label == CHECK_OUT_TIME_LABEL:
                found_out_label = True
                out_time = value
        except Exception:
            continue

    if not found_in_label:
        in_time = _find_labeled_clock_time(driver, CHECK_IN_TIME_LABEL)
    if not found_out_label:
        out_time = _find_labeled_clock_time(driver, CHECK_OUT_TIME_LABEL)
    return in_time, out_time


def has_recorded_check_in_time(driver: webdriver.Chrome) -> bool:
    return _read_clock_times(driver)[0] is not None


def has_recorded_check_out_time(driver: webdriver.Chrome) -> bool:
    return _read_clock_times(driver)[1] is not None


def _read_attendance_snapshot(driver: webdriver.Chrome) -> dict:
    """시각·버튼 상태를 한 번에 읽어 peek/verify가 공유한다."""
    in_time, out_time = _read_clock_times(driver)
    check_in = find_button_by_xpaths(
        driver, CHECK_IN_BUTTON_XPATHS, require_enabled=False
    )
    check_out = find_button_by_xpaths(
        driver, CHECK_OUT_BUTTON_XPATHS, require_enabled=False
    )
    check_in_enabled = bool(check_in is not None and _is_effectively_enabled(check_in))
    check_out_enabled = bool(
        check_out is not None and _is_effectively_enabled(check_out)
    )
    return {
        "in_time": in_time,
        "out_time": out_time,
        "check_in_present": check_in is not None,
        "check_out_present": check_out is not None,
        "check_in_enabled": check_in_enabled,
        "check_out_enabled": check_out_enabled,
    }


def _ui_state_from_snapshot(snap: dict) -> AttendanceUiState:
    if snap.get("out_time"):
        return "checked_out"
    if snap.get("in_time"):
        return "checked_in"
    if snap.get("check_in_enabled") and not snap.get("check_out_enabled"):
        return "not_checked_in"
    if snap.get("check_out_enabled"):
        return "checked_in"
    if snap.get("check_out_present") and not snap.get("check_out_enabled"):
        if (not snap.get("check_in_present")) or (not snap.get("check_in_enabled")):
            return "checked_out"
    return "unknown"


def peek_attendance_ui_state(driver: webdriver.Chrome) -> AttendanceUiState:
    """서버 UI 상태 판정: 기록된 시각 우선, 없으면 활성 버튼으로 판정."""
    if not wait_for_attendance_url(driver):
        return "unknown"
    return _ui_state_from_snapshot(_read_attendance_snapshot(driver))


def _check_in_verified(driver: webdriver.Chrome) -> bool:
    snap = _read_attendance_snapshot(driver)
    if snap.get("in_time") or snap.get("check_out_enabled"):
        return True
    return not snap.get("check_in_enabled")


def _check_out_verified(driver: webdriver.Chrome) -> bool:
    """퇴근 시간이 채워졌거나, 활성 퇴근 버튼이 비활성으로 바뀐 경우 성공."""
    snap = _read_attendance_snapshot(driver)
    if snap.get("out_time"):
        return True
    if snap.get("check_out_enabled"):
        return False
    if snap.get("check_out_present") and not snap.get("check_out_enabled"):
        return True
    # 버튼이 아직 안 뜬 상태에서는 성공으로 보지 않음
    if not snap.get("check_in_present") and not snap.get("check_out_present"):
        return False
    return not snap.get("check_in_enabled")


def _log_attendance_debug(driver: webdriver.Chrome, label: str) -> None:
    """검증 실패 시 버튼·토스트·기록 시각을 남겨 원인 추적을 돕는다."""
    try:
        snap = _read_attendance_snapshot(driver)
        in_state = "none"
        out_state = "none"
        if snap.get("check_in_present"):
            in_state = "enabled" if snap.get("check_in_enabled") else "disabled"
        if snap.get("check_out_present"):
            out_state = "enabled" if snap.get("check_out_enabled") else "disabled"

        messages: list[str] = []
        for xpath in (
            "//*[@role='alert']",
            "//*[contains(@class,'toast')]",
            "//*[contains(@class,'snackbar')]",
            "//*[contains(@class,'message')]",
            f"//{_DIALOG_ROOT}",
        ):
            try:
                for element in driver.find_elements(By.XPATH, xpath):
                    try:
                        if not element.is_displayed():
                            continue
                        text = _normalize_ui_text(element.text)
                        if text and text not in messages:
                            messages.append(text[:160])
                    except Exception:
                        continue
            except Exception:
                continue
            if len(messages) >= 5:
                break

        logging.warning(
            "%s 검증 실패 진단 — check_in=%s check_out=%s "
            "in_time=%s out_time=%s url=%s messages=%s",
            label,
            in_state,
            out_state,
            snap.get("in_time") or "-",
            snap.get("out_time") or "-",
            driver.current_url,
            messages[:5] or "(none)",
        )
    except Exception:
        logging.exception("%s 검증 실패 진단 로깅 중 오류", label)


def _verify_after_click(
    driver: webdriver.Chrome,
    action: Literal["check_in", "check_out"],
) -> Literal["success", "failed", "unknown"]:
    """클릭 후 DOM 검증. 실패 시에만 refresh 후 재확인."""
    predicate = (
        _check_in_verified if action == "check_in" else _check_out_verified
    )
    label = "출근" if action == "check_in" else "퇴근"

    _handle_post_click_prompts(driver, timeout_sec=1.5)

    try:
        WebDriverWait(driver, VERIFY_WAIT_SEC).until(predicate)
        logging.info("%s DOM 검증 성공", label)
        return "success"
    except TimeoutException:
        logging.warning("%s DOM 검증 타임아웃 (refresh 전)", label)
        _log_attendance_debug(driver, f"{label}(refresh 전)")
    except WebDriverException:
        logging.exception("%s DOM 검증 중 WebDriver 오류", label)
        return "unknown"

    try:
        driver.refresh()
        time.sleep(0.8)
        if not wait_for_attendance_url(driver):
            return "unknown"
        WebDriverWait(driver, VERIFY_REFRESH_WAIT_SEC).until(predicate)
        logging.info("%s DOM 검증 성공 (refresh 후)", label)
        return "success"
    except TimeoutException:
        logging.warning("%s DOM 검증 실패 (refresh 후)", label)
        _log_attendance_debug(driver, f"{label}(refresh 후)")
        return "failed"
    except WebDriverException:
        logging.exception("%s refresh 검증 중 WebDriver 오류", label)
        return "unknown"


def click_check_in_button(driver: webdriver.Chrome) -> bool:
    today = date.today()
    if not should_attempt_check_in(today):
        return False

    if not _click_labeled_button(
        driver, xpaths=CHECK_IN_BUTTON_XPATHS, label="출근하기"
    ):
        record_failure(
            "check_in",
            "button_not_found",
            "출근하기 버튼을 찾지 못했거나 클릭에 실패했습니다",
        )
        notify_attendance_failure(
            title="출근 체크 실패",
            message="출근 버튼을 찾지 못했습니다. 잠시 후 재시도합니다.",
        )
        return False

    result = _verify_after_click(driver, "check_in")
    if result == "success":
        save_check_in_date(today)
        notify_check_in_done(today)
        return True

    if result == "unknown":
        record_failure(
            "check_in",
            "verify_unknown",
            "출근 클릭 후 상태를 확인하지 못했습니다",
            result="unknown",
        )
        notify_attendance_failure(
            title="출근 상태 불명확",
            message="클릭은 되었으나 서버 상태를 확인하지 못했습니다.",
        )
    else:
        record_failure(
            "check_in",
            "verify_failed",
            "출근 클릭 후 DOM 검증에 실패했습니다",
        )
        notify_attendance_failure(
            title="출근 체크 실패",
            message="출근이 반영되지 않은 것으로 보입니다. 재시도합니다.",
        )
    return False


def click_check_out_button(driver: webdriver.Chrome) -> bool:
    today = date.today()
    if load_last_check_out_date() == today:
        logging.info("오늘 이미 퇴근 처리됨 — 클릭 생략")
        return False

    if not _click_labeled_button(
        driver, xpaths=CHECK_OUT_BUTTON_XPATHS, label="퇴근하기"
    ):
        record_failure(
            "check_out",
            "button_not_found",
            "퇴근하기 버튼을 찾지 못했거나 클릭에 실패했습니다",
        )
        notify_attendance_failure(
            title="퇴근 체크 실패",
            message="퇴근 버튼을 찾지 못했습니다. 잠시 후 재시도합니다.",
        )
        return False

    result = _verify_after_click(driver, "check_out")
    if result == "success":
        save_check_out_date(today)
        notify_check_out_done(today)
        return True

    if result == "unknown":
        record_failure(
            "check_out",
            "verify_unknown",
            "퇴근 클릭 후 상태를 확인하지 못했습니다",
            result="unknown",
        )
        notify_attendance_failure(
            title="퇴근 상태 불명확",
            message="클릭은 되었으나 서버 상태를 확인하지 못했습니다.",
        )
    else:
        record_failure(
            "check_out",
            "verify_failed",
            "퇴근 클릭 후 DOM 검증에 실패했습니다",
        )
        notify_attendance_failure(
            title="퇴근 체크 실패",
            message="퇴근이 반영되지 않은 것으로 보입니다. 재시도합니다.",
        )
    return False


def login_if_needed(
    driver: webdriver.Chrome,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> bool:
    wait = WebDriverWait(driver, 30)
    try:
        wait.until(
            lambda d: _find_visible(d, "input[type='password']") is not None
            or (
                "my-attendance-status" in d.current_url
                and "/login" not in d.current_url
            )
        )
    except TimeoutException:
        logging.warning("페이지 로딩 시간 초과: %s", driver.current_url)
        return False

    time.sleep(0.5)
    if (
        "my-attendance-status" in driver.current_url
        and "/login" not in driver.current_url
        and _find_visible(driver, "input[type='password']") is None
    ):
        logging.info("이미 로그인된 세션 — 로그인 생략")
        return True

    password_input = _find_visible(driver, "input[type='password']")
    username_input = _find_visible(driver, "input.input_txt[type='text']")
    if username_input is None:
        username_input = _find_visible(driver, "input[type='text']")

    if password_input is None or username_input is None:
        logging.error("로그인 입력란을 찾지 못함 (url=%s)", driver.current_url)
        return False

    logging.info("로그인 폼 감지 — 계정 정보 입력 시작")
    if username is None or password is None:
        try:
            username, password = load_login_credentials()
        except Exception:
            logging.exception("로그인 설정 로드 실패")
            return False

    _set_input_value(driver, username_input, username)
    _set_input_value(driver, password_input, password)

    login_button = None
    for xpath in (
        "//button[@type='submit' and contains(., '로그인')]",
        "//button[contains(., '로그인')]",
        "//button[@type='submit' and contains(@class, 'solid')]",
    ):
        for button in driver.find_elements(By.XPATH, xpath):
            try:
                if button.is_displayed() and button.is_enabled():
                    login_button = button
                    break
            except Exception:
                continue
        if login_button is not None:
            break

    if login_button is None:
        logging.error("로그인 버튼을 찾지 못함")
        return False

    login_button.click()
    logging.info("로그인 버튼 클릭")

    try:
        WebDriverWait(driver, ATTENDANCE_PAGE_WAIT_SEC).until(
            lambda d: "/login" not in d.current_url
            and _find_visible(d, "input[type='password']") is None
        )
        logging.info("자동 로그인 성공: %s", driver.current_url)
        return True
    except TimeoutException:
        logging.warning(
            "로그인 후 페이지 전환 확인 실패 (url=%s)",
            driver.current_url,
        )
        return False


def wait_for_attendance_buttons(
    driver: webdriver.Chrome,
    attendance_url: Optional[str] = None,
) -> bool:
    if not wait_for_attendance_url(driver, attendance_url=attendance_url):
        return False
    logging.info("근태 URL 도착 — 출근/퇴근 버튼 렌더 대기")
    try:
        WebDriverWait(driver, CHECK_IN_RENDER_WAIT_SEC).until(
            lambda d: find_button_by_xpaths(
                d, ATTENDANCE_ACTION_XPATHS, require_enabled=False
            )
            is not None
        )
        logging.info("출근/퇴근 버튼 확인 완료: %s", driver.current_url)
        return True
    except TimeoutException:
        logging.error(
            "출근/퇴근 버튼 대기 시간 초과(%s초): %s",
            CHECK_IN_RENDER_WAIT_SEC,
            driver.current_url,
        )
        return False


def _run_with_driver(worker) -> None:
    if not _attendance_lock.acquire(blocking=False):
        logging.info("근태 작업 진행 중 — 생략")
        return

    chrome = find_chrome_executable()
    if chrome is None:
        logging.error("Chrome 실행 파일을 찾을 수 없음")
        _attendance_lock.release()
        return

    driver: Optional[webdriver.Chrome] = None
    try:
        attendance_url = load_attendance_url()
        driver = webdriver.Chrome(
            service=ChromeService(),
            options=create_chrome_options(chrome),
        )
        _active_drivers.append(driver)
        driver.get(attendance_url)
        worker(driver)
    except ValueError:
        logging.exception("근태 URL/설정 로드 실패")
    except WebDriverException as exc:
        logging.exception("근태 작업 중 WebDriver 오류")
        action = getattr(worker, "_attendance_action", None)
        if action in ("check_in", "check_out"):
            record_failure(
                action,
                "network",
                f"WebDriver: {exc}",
            )
            notify_attendance_failure(
                title="근태 연결 실패",
                message="브라우저/네트워크 오류로 작업을 마치지 못했습니다.",
            )
    except Exception:
        logging.exception("근태 작업 실패")
    finally:
        _close_browser(driver)
        _attendance_lock.release()


def _auto_login_worker(_chrome: Path | None = None) -> None:
    def _job(driver: webdriver.Chrome) -> None:
        if not login_if_needed(driver):
            record_failure(
                "check_in",
                "auth",
                "로그인에 실패했습니다",
            )
            notify_attendance_failure(
                title="로그인 실패",
                message="아이디/비밀번호를 확인하세요. 자동 재시도를 중단합니다.",
            )
            return
        logging.info("근태 화면 전환 대기 후 출근하기 진행")
        click_check_in_button(driver)

    _job._attendance_action = "check_in"  # type: ignore[attr-defined]
    _run_with_driver(_job)


def _auto_checkout_worker(_chrome: Path | None = None) -> None:
    def _job(driver: webdriver.Chrome) -> None:
        today = date.today()
        if not login_if_needed(driver):
            record_failure(
                "check_out",
                "auth",
                "로그인에 실패했습니다",
            )
            notify_attendance_failure(
                title="로그인 실패",
                message="아이디/비밀번호를 확인하세요. 자동 재시도를 중단합니다.",
            )
            return

        logging.info("근태 화면 전환 후 서버 상태 peek")
        ui_state = peek_attendance_ui_state(driver)
        logging.info("서버 근태 UI 상태: %s", ui_state)

        if ui_state == "checked_out":
            logging.info("서버에 이미 퇴근됨 — 로컬 상태 동기화")
            save_check_out_date(today)
            return
        if ui_state == "not_checked_in":
            logging.info("서버 미출근 — 자동 퇴근 생략 (이후 재시도 허용)")
            request_checkout_rearm()
            return
        if ui_state == "unknown":
            record_failure(
                "check_out",
                "verify_unknown",
                "서버 근태 버튼 상태를 판별하지 못했습니다",
                result="unknown",
            )
            notify_attendance_failure(
                title="퇴근 상태 불명확",
                message="서버 근태 상태를 확인하지 못했습니다.",
            )
            return

        # checked_in
        logging.info("서버 출근 확인 — 퇴근하기 진행")
        click_check_out_button(driver)

    _job._attendance_action = "check_out"  # type: ignore[attr-defined]
    _run_with_driver(_job)


def open_attendance_page() -> bool:
    if not has_app_setup():
        logging.warning("앱 설정 없음 — 웹창 실행 생략")
        return False

    allowed, reason = should_open_browser()
    if not allowed:
        logging.info("웹창 실행 생략 — 사유: %s", reason)
        return False

    if find_chrome_executable() is None:
        logging.error("Chrome 실행 파일을 찾을 수 없음")
        return False

    try:
        attendance_url = load_attendance_url()
    except Exception:
        logging.exception("근태 URL 로드 실패")
        return False

    threading.Thread(
        target=_auto_login_worker,
        name="auto-login",
        daemon=True,
    ).start()
    logging.info("근태 페이지 자동 로그인 시작: %s", attendance_url)
    return True


def open_checkout_page() -> bool:
    global _checkout_job_running

    if not has_app_setup():
        logging.warning("앱 설정 없음 — 자동 퇴근 생략")
        return False

    if find_chrome_executable() is None:
        logging.error("Chrome 실행 파일을 찾을 수 없음")
        return False

    if _checkout_job_running:
        logging.info("자동 퇴근 작업이 이미 진행 중")
        return False

    try:
        attendance_url = load_attendance_url()
    except Exception:
        logging.exception("근태 URL 로드 실패")
        return False

    _checkout_job_running = True

    def _runner() -> None:
        global _checkout_job_running
        try:
            _auto_checkout_worker()
        finally:
            _checkout_job_running = False

    threading.Thread(target=_runner, name="auto-checkout", daemon=True).start()
    logging.info("자동 퇴근 시작: %s", attendance_url)
    return True


def verify_login_credentials(
    username: str,
    password: str,
    *,
    attendance_url: Optional[str] = None,
) -> tuple[bool, str]:
    from startofwork.config import (
        is_missing_attendance_url,
        is_missing_credentials,
        normalize_attendance_url,
        normalize_credential,
    )

    username = normalize_credential(username)
    password = str(password or "").strip()
    if is_missing_credentials(username, password):
        return False, "아이디와 비밀번호를 입력하세요"

    if attendance_url is None:
        try:
            target_url = load_attendance_url()
        except Exception:
            return False, "근태 페이지 URL이 설정되어 있지 않습니다."
    else:
        target_url = normalize_attendance_url(attendance_url)
        if is_missing_attendance_url(target_url):
            return False, "근태 페이지 URL을 http(s):// 형식으로 입력하세요."

    chrome = find_chrome_executable()
    if chrome is None:
        return False, "Chrome 실행 파일을 찾을 수 없습니다"

    if not _attendance_lock.acquire(blocking=False):
        return False, "다른 근태 작업이 진행 중입니다. 잠시 후 다시 시도하세요."

    driver: Optional[webdriver.Chrome] = None
    try:
        driver = webdriver.Chrome(
            service=ChromeService(),
            options=create_chrome_options(chrome),
        )
        _active_drivers.append(driver)
        driver.get(target_url)
        if not login_if_needed(driver, username=username, password=password):
            return False, "로그인에 실패했습니다. 아이디/비밀번호를 확인하세요."
        if not wait_for_attendance_buttons(driver, attendance_url=target_url):
            return (
                False,
                "로그인은 되었지만 근태 페이지(출근/퇴근 버튼)를 확인할 수 없습니다.",
            )
        return True, "로그인 및 근태 페이지 확인 완료"
    except WebDriverException:
        logging.exception("로그인 검증 중 WebDriver 오류")
        return False, "브라우저 오류가 발생했습니다. 로그를 확인하세요."
    except Exception:
        logging.exception("로그인 검증 실패")
        return False, "로그인 확인 중 오류가 발생했습니다."
    finally:
        _close_browser(driver)
        _attendance_lock.release()