"""Chrome(Selenium) 기반 근태 로그인·출근·퇴근 자동화."""

from __future__ import annotations

import logging
import os
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

_active_drivers: list[webdriver.Chrome] = []
_attendance_lock = threading.Lock()
_checkout_job_running = False
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


def is_checkout_job_running() -> bool:
    return _checkout_job_running


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
                    if require_enabled and not target.is_enabled():
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
    time.sleep(0.5)
    driver.execute_script(
        "arguments[0].scrollIntoView({block: 'center'});",
        target,
    )
    time.sleep(0.2)
    try:
        target.click()
    except Exception:
        driver.execute_script("arguments[0].click();", target)


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
        time.sleep(1.5)
        return True
    except Exception:
        logging.exception("%s 버튼 클릭 실패", label)
        return False


def peek_attendance_ui_state(driver: webdriver.Chrome) -> AttendanceUiState:
    """활성 출근/퇴근 버튼 존재로 서버 UI 상태 판정 (클릭 없음)."""
    if not wait_for_attendance_url(driver):
        return "unknown"

    check_in = find_button_by_xpaths(driver, CHECK_IN_BUTTON_XPATHS)
    check_out = find_button_by_xpaths(driver, CHECK_OUT_BUTTON_XPATHS)

    if check_in is not None and check_out is None:
        return "not_checked_in"
    if check_out is not None:
        return "checked_in"
    if check_in is None and check_out is None:
        # 둘 다 비활성/없음 — 이미 퇴근했을 가능성
        disabled_out = find_button_by_xpaths(
            driver, CHECK_OUT_BUTTON_XPATHS, require_enabled=False
        )
        disabled_in = find_button_by_xpaths(
            driver, CHECK_IN_BUTTON_XPATHS, require_enabled=False
        )
        if disabled_out is not None and disabled_in is None:
            return "checked_out"
        if disabled_in is not None and (
            disabled_out is None or not disabled_out.is_enabled()
        ):
            # 출근 버튼만 보이는데 비활성 → 모호
            if disabled_out is not None and not disabled_out.is_enabled():
                return "checked_out"
        return "unknown"
    return "unknown"


def _check_in_verified(driver: webdriver.Chrome) -> bool:
    check_in = find_button_by_xpaths(driver, CHECK_IN_BUTTON_XPATHS)
    check_out = find_button_by_xpaths(driver, CHECK_OUT_BUTTON_XPATHS)
    return check_in is None or check_out is not None


def _check_out_verified(driver: webdriver.Chrome) -> bool:
    return find_button_by_xpaths(driver, CHECK_OUT_BUTTON_XPATHS) is None


def _verify_after_click(
    driver: webdriver.Chrome,
    action: Literal["check_in", "check_out"],
) -> Literal["success", "failed", "unknown"]:
    """클릭 후 DOM 검증 → refresh → 재확인."""
    predicate = (
        _check_in_verified if action == "check_in" else _check_out_verified
    )
    label = "출근" if action == "check_in" else "퇴근"

    try:
        WebDriverWait(driver, VERIFY_WAIT_SEC).until(predicate)
    except TimeoutException:
        logging.warning("%s DOM 검증 타임아웃 (refresh 전)", label)
        # refresh 후에도 실패면 unknown/failed 판정
    except WebDriverException:
        logging.exception("%s DOM 검증 중 WebDriver 오류", label)
        return "unknown"

    try:
        driver.refresh()
        time.sleep(1.0)
        if not wait_for_attendance_url(driver):
            return "unknown"
        WebDriverWait(driver, VERIFY_REFRESH_WAIT_SEC).until(predicate)
        logging.info("%s DOM 검증 성공 (refresh 후)", label)
        return "success"
    except TimeoutException:
        logging.warning("%s DOM 검증 실패 (refresh 후)", label)
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
            logging.info("서버 미출근 — 자동 퇴근 생략")
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