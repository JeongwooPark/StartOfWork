"""Chrome(Selenium) 기반 근태 로그인·출근·퇴근 자동화."""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Optional

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
    save_check_in_date,
    save_check_out_date,
)
from startofwork.config import has_login_credentials, load_login_credentials
from startofwork.constants import (
    ATTENDANCE_PAGE_WAIT_SEC,
    ATTENDANCE_URL,
    CHECK_IN_BUTTON_XPATH,
    CHECK_IN_RENDER_WAIT_SEC,
    CHECK_OUT_BUTTON_XPATH,
)
from startofwork.notifications import notify_check_in_done, notify_check_out_done
from startofwork.paths import CHROME_PROFILE_DIR
from startofwork.rules import should_attempt_check_in, should_open_browser

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


def wait_for_attendance_url(driver: webdriver.Chrome) -> bool:
    if "my-attendance-status" not in driver.current_url:
        logging.info("근태 화면으로 이동 중...")
        driver.get(ATTENDANCE_URL)
    try:
        WebDriverWait(driver, ATTENDANCE_PAGE_WAIT_SEC).until(
            lambda d: "my-attendance-status" in d.current_url
            and "/login" not in d.current_url
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


def click_check_in_button(driver: webdriver.Chrome) -> bool:
    today = date.today()
    if not should_attempt_check_in(today):
        return False
    if not _click_labeled_button(
        driver, xpaths=CHECK_IN_BUTTON_XPATHS, label="출근하기"
    ):
        return False
    save_check_in_date(today)
    notify_check_in_done(today)
    return True


def click_check_out_button(driver: webdriver.Chrome) -> bool:
    today = date.today()
    if load_last_check_out_date() == today:
        logging.info("오늘 이미 퇴근 처리됨 — 클릭 생략")
        return False
    if not _click_labeled_button(
        driver, xpaths=CHECK_OUT_BUTTON_XPATHS, label="퇴근하기"
    ):
        return False
    save_check_out_date(today)
    notify_check_out_done(today)
    return True


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


def wait_for_attendance_buttons(driver: webdriver.Chrome) -> bool:
    if not wait_for_attendance_url(driver):
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
        driver = webdriver.Chrome(
            service=ChromeService(),
            options=create_chrome_options(chrome),
        )
        _active_drivers.append(driver)
        driver.get(ATTENDANCE_URL)
        worker(driver)
    except WebDriverException:
        logging.exception("근태 작업 중 WebDriver 오류")
    except Exception:
        logging.exception("근태 작업 실패")
    finally:
        _close_browser(driver)
        _attendance_lock.release()


def _auto_login_worker(_chrome: Path | None = None) -> None:
    def _job(driver: webdriver.Chrome) -> None:
        if not login_if_needed(driver):
            return
        logging.info("근태 화면 전환 대기 후 출근하기 진행")
        click_check_in_button(driver)

    _run_with_driver(_job)


def _auto_checkout_worker(_chrome: Path | None = None) -> None:
    def _job(driver: webdriver.Chrome) -> None:
        if not login_if_needed(driver):
            return
        logging.info("근태 화면 전환 대기 후 퇴근하기 진행")
        click_check_out_button(driver)

    _run_with_driver(_job)


def open_attendance_page() -> bool:
    if not has_login_credentials():
        logging.warning("로그인 설정 없음 — 웹창 실행 생략")
        return False

    allowed, reason = should_open_browser()
    if not allowed:
        logging.info("웹창 실행 생략 — 사유: %s", reason)
        return False

    if find_chrome_executable() is None:
        logging.error("Chrome 실행 파일을 찾을 수 없음")
        return False

    threading.Thread(
        target=_auto_login_worker,
        name="auto-login",
        daemon=True,
    ).start()
    logging.info("근태 페이지 자동 로그인 시작: %s", ATTENDANCE_URL)
    return True


def open_checkout_page() -> bool:
    global _checkout_job_running

    if not has_login_credentials():
        logging.warning("로그인 설정 없음 — 자동 퇴근 생략")
        return False

    if find_chrome_executable() is None:
        logging.error("Chrome 실행 파일을 찾을 수 없음")
        return False

    if _checkout_job_running:
        logging.info("자동 퇴근 작업이 이미 진행 중")
        return False

    _checkout_job_running = True

    def _runner() -> None:
        global _checkout_job_running
        try:
            _auto_checkout_worker()
        finally:
            _checkout_job_running = False

    threading.Thread(target=_runner, name="auto-checkout", daemon=True).start()
    logging.info("자동 퇴근 시작: %s", ATTENDANCE_URL)
    return True


def verify_login_credentials(username: str, password: str) -> tuple[bool, str]:
    from startofwork.config import is_missing_credentials, normalize_credential

    username = normalize_credential(username)
    password = str(password or "").strip()
    if is_missing_credentials(username, password):
        return False, "아이디와 비밀번호를 입력하세요"

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
        driver.get(ATTENDANCE_URL)
        if not login_if_needed(driver, username=username, password=password):
            return False, "로그인에 실패했습니다. 아이디/비밀번호를 확인하세요."
        if not wait_for_attendance_buttons(driver):
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
