"""Chrome(Selenium) 기반 근태 로그인·출근·퇴근 자동화."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Literal, Optional

from selenium import webdriver
from selenium.common.exceptions import (
    SessionNotCreatedException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
import selenium.webdriver.chrome.webdriver  # noqa: F401
import selenium.webdriver.chromium.webdriver  # noqa: F401
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from startofwork.attendance_state import (
    load_last_check_in_date,
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
    LOGIN_FORM_WAIT_SEC,
    PAGE_LOAD_TIMEOUT_SEC,
)
from startofwork.notifications import (
    notify_attendance_failure,
    notify_check_in_done,
    notify_check_out_done,
)
from startofwork.lock_state import get_windows_lock_state
from startofwork.paths import CHROME_PROFILE_DIR
from startofwork.rules import should_attempt_check_in, should_open_browser

AttendanceUiState = Literal[
    "not_checked_in", "checked_in", "checked_out", "unknown"
]
LoginOutcome = Literal["ok", "auth", "network"]

VERIFY_WAIT_SEC = 15
VERIFY_REFRESH_WAIT_SEC = 8
CONFIRM_PROMPT_WAIT_SEC = 3.0
LOGIN_FORM_RETRY_WAIT_SEC = 25

PASSWORD_INPUT_SELECTORS = (
    "input[type='password']",
    "input[autocomplete='current-password']",
    "input[name='password']",
)
USERNAME_INPUT_SELECTORS = (
    "input.input_txt[type='text']",
    "input[type='email']",
    "input[autocomplete='username']",
    "input[name='username']",
    "input[name='id']",
    "input[type='text']",
)

_active_drivers: list[webdriver.Chrome] = []
_attendance_lock = threading.Lock()
_checkout_job_running = False
_checkout_rearm_requested = False
_cached_chrome: Optional[Path] = None
_chrome_resolved = False
_temp_chrome_profiles: list[Path] = []
_CHROME_LOCK_FILES = (
    "DevToolsActivePort",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "lockfile",
)
HeadlessMode = Literal["new", "old"]
_LOCK_UNSET = object()

HEADED_RESULT_HOLD_SEC = 4.0

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


def create_chrome_options(
    chrome: Path,
    *,
    profile_dir: Optional[Path] = None,
    headless: Optional[HeadlessMode] = "new",
) -> ChromeOptions:
    options = ChromeOptions()
    options.binary_location = str(chrome)
    # complete 대기는 추적 스크립트 행에 막히기 쉽다. DOM 이후 폼을 직접 기다린다.
    options.page_load_strategy = "eager"
    target_profile = profile_dir if profile_dir is not None else CHROME_PROFILE_DIR
    target_profile.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={target_profile}")
    if headless == "old":
        options.add_argument("--headless=old")
    elif headless == "new":
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--mute-audio")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-port=0")
    options.add_argument(
        "--disable-features=VizDisplayCompositor,CalculateNativeWinOcclusion"
    )
    options.add_experimental_option("excludeSwitches", ["enable-logging"])
    return options


def _webdriver_error_summary(exc: BaseException) -> str:
    raw = str(exc).strip()
    text = raw.splitlines()[0] if raw else exc.__class__.__name__
    if len(text) > 240:
        return text[:237] + "..."
    return text


def _is_chrome_start_failure(exc: BaseException) -> bool:
    if isinstance(exc, SessionNotCreatedException):
        return True
    if not isinstance(exc, WebDriverException):
        return False
    msg = str(exc).lower()
    needles = (
        "session not created",
        "devtoolsactiveport",
        "chrome failed to start",
        "chrome not reachable",
        "unable to discover open pages",
    )
    return any(needle in msg for needle in needles)


def _headless_mode_order(
    locked: Optional[bool] | object = _LOCK_UNSET,
) -> tuple[HeadlessMode, HeadlessMode]:
    """잠긴 세션에서는 new headless가 GPU/컴포지터 크래시를 내는 경우가 많다."""
    if locked is _LOCK_UNSET:
        locked = get_windows_lock_state()
    if locked is False:
        return ("new", "old")
    return ("old", "new")


def _clear_stale_chrome_locks(profile_dir: Path) -> None:
    for name in _CHROME_LOCK_FILES:
        path = profile_dir / name
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                logging.info("Chrome 잔여 잠금 파일 삭제: %s", name)
        except OSError:
            logging.debug("Chrome 잠금 파일 삭제 실패: %s", path)


def _kill_chrome_using_profile(profile_dir: Path) -> int:
    """우리 chrome_profile을 쓰는 잔여 chrome.exe만 종료. 사용자 Chrome은 건드리지 않음."""
    if sys.platform != "win32":
        return 0
    try:
        marker = str(profile_dir.resolve())
    except OSError:
        marker = str(profile_dir)
    if not marker:
        return 0
    escaped = marker.replace("'", "''")
    ps = (
        f"$m = '{escaped}'; $n = 0; "
        "$procs = Get-CimInstance Win32_Process -Filter \"Name = 'chrome.exe'\" "
        "-ErrorAction SilentlyContinue; "
        "foreach ($p in $procs) { "
        "  if ($p.CommandLine -and "
        "      ($p.CommandLine.IndexOf($m, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { "
        "    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; $n++ } "
        "    catch {} "
        "  } "
        "}; Write-Output $n"
    )
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            check=False,
            timeout=8,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired):
        logging.warning("전용 프로필 Chrome 프로세스 정리 실패")
        return 0
    lines = (result.stdout or "").strip().splitlines()
    killed = 0
    if lines:
        try:
            killed = int(lines[-1])
        except ValueError:
            killed = 0
    if killed:
        logging.info("전용 프로필 Chrome 잔여 프로세스 종료: %s개", killed)
        time.sleep(0.4)
    return killed


def _prepare_chrome_profile(profile_dir: Path) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    _kill_chrome_using_profile(profile_dir)
    _clear_stale_chrome_locks(profile_dir)


def _cleanup_temp_chrome_profiles() -> None:
    while _temp_chrome_profiles:
        path = _temp_chrome_profiles.pop()
        shutil.rmtree(path, ignore_errors=True)


def is_attendance_job_running() -> bool:
    return _attendance_lock.locked()


def _create_driver(chrome: Path, *, headed: bool = False) -> webdriver.Chrome:
    profile = CHROME_PROFILE_DIR
    _prepare_chrome_profile(profile)
    if headed:
        logging.info("Chrome 세션 시작 — headed")
        last_exc: Optional[WebDriverException] = None
        try:
            driver = webdriver.Chrome(
                service=ChromeService(),
                options=create_chrome_options(chrome, headless=None),
            )
            logging.info("Chrome 세션 생성 완료 (headed)")
            return _configure_driver(driver)
        except WebDriverException as exc:
            last_exc = exc
            if not _is_chrome_start_failure(exc):
                raise
            logging.warning(
                "Chrome 기동 실패 (headed): %s",
                _webdriver_error_summary(exc),
            )
        temp_profile = Path(tempfile.mkdtemp(prefix="startofwork_chrome_"))
        _temp_chrome_profiles.append(temp_profile)
        logging.warning(
            "전용 프로필 기동 실패 — 임시 프로필로 재시도: %s", temp_profile
        )
        try:
            driver = webdriver.Chrome(
                service=ChromeService(),
                options=create_chrome_options(
                    chrome, profile_dir=temp_profile, headless=None
                ),
            )
            logging.info("Chrome 세션 생성 완료 (headed, temp-profile)")
            return _configure_driver(driver)
        except WebDriverException:
            _cleanup_temp_chrome_profiles()
            if last_exc is not None:
                raise last_exc
            raise

    locked = get_windows_lock_state()
    modes = _headless_mode_order(locked)
    logging.info(
        "Chrome 세션 시작 — headless 순서=%s (잠금=%s)",
        ",".join(modes),
        locked,
    )

    last_exc: Optional[WebDriverException] = None
    for index, headless in enumerate(modes):
        if index > 0:
            _prepare_chrome_profile(profile)
        try:
            driver = webdriver.Chrome(
                service=ChromeService(),
                options=create_chrome_options(chrome, headless=headless),
            )
            logging.info("Chrome 세션 생성 완료 (headless=%s)", headless)
            return _configure_driver(driver)
        except WebDriverException as exc:
            last_exc = exc
            if not _is_chrome_start_failure(exc):
                raise
            logging.warning(
                "Chrome 기동 실패 (headless=%s): %s",
                headless,
                _webdriver_error_summary(exc),
            )

    temp_profile = Path(tempfile.mkdtemp(prefix="startofwork_chrome_"))
    _temp_chrome_profiles.append(temp_profile)
    logging.warning("전용 프로필 기동 실패 — 임시 프로필로 재시도: %s", temp_profile)
    try:
        driver = webdriver.Chrome(
            service=ChromeService(),
            options=create_chrome_options(
                chrome, profile_dir=temp_profile, headless="old"
            ),
        )
        logging.info("Chrome 세션 생성 완료 (headless=old, temp-profile)")
        return _configure_driver(driver)
    except WebDriverException:
        _cleanup_temp_chrome_profiles()
        if last_exc is not None:
            raise last_exc
        raise


def _configure_driver(driver: webdriver.Chrome) -> webdriver.Chrome:
    try:
        driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT_SEC)
    except Exception:
        logging.debug("pageLoadTimeout 설정 실패", exc_info=True)
    return driver


def _goto_url(driver: webdriver.Chrome, url: str) -> None:
    try:
        driver.get(url)
    except TimeoutException:
        logging.warning(
            "페이지 로드 타임아웃(%ss) — 현재 URL로 계속: %s",
            PAGE_LOAD_TIMEOUT_SEC,
            driver.current_url,
        )


def _close_browser(driver: Optional[webdriver.Chrome]) -> None:
    try:
        if driver is not None:
            try:
                driver.quit()
                logging.info("Chrome 창 종료 완료")
            except Exception:
                logging.exception("Chrome 창 종료 실패")
            finally:
                if driver in _active_drivers:
                    _active_drivers.remove(driver)
    finally:
        _cleanup_temp_chrome_profiles()


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


def _find_input(
    driver: webdriver.Chrome,
    selectors: tuple[str, ...],
    *,
    visible_only: bool = True,
):
    for css in selectors:
        found = _find_visible(driver, css)
        if found is not None:
            return found
    if visible_only:
        return None
    for css in selectors:
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, css)
        except Exception:
            continue
        if elements:
            return elements[0]
    return None


def _find_password_input(
    driver: webdriver.Chrome, *, visible_only: bool = True
):
    return _find_input(
        driver, PASSWORD_INPUT_SELECTORS, visible_only=visible_only
    )


def _find_username_input(
    driver: webdriver.Chrome, *, visible_only: bool = True
):
    return _find_input(
        driver, USERNAME_INPUT_SELECTORS, visible_only=visible_only
    )


def _is_attendance_url(driver: webdriver.Chrome) -> bool:
    url = driver.current_url or ""
    return "my-attendance-status" in url and "/login" not in url


def _login_surface_ready(driver: webdriver.Chrome) -> bool:
    if _find_password_input(driver, visible_only=False) is not None:
        return True
    return _is_attendance_url(driver)


def _wait_for_login_surface(driver: webdriver.Chrome, timeout_sec: float) -> bool:
    try:
        WebDriverWait(driver, timeout_sec).until(_login_surface_ready)
        return True
    except TimeoutException:
        return False


def _log_login_page_debug(driver: webdriver.Chrome) -> None:
    try:
        url = driver.current_url or ""
        title = ""
        ready = ""
        try:
            title = (driver.title or "").strip()[:120]
        except Exception:
            title = "?"
        try:
            ready = str(driver.execute_script("return document.readyState") or "")
        except Exception:
            ready = "?"
        n_input = 0
        n_password = 0
        n_iframe = 0
        try:
            n_input = len(driver.find_elements(By.CSS_SELECTOR, "input"))
            n_password = len(
                driver.find_elements(By.CSS_SELECTOR, "input[type='password']")
            )
            n_iframe = len(driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
        except Exception:
            pass
        logging.warning(
            "로그인 페이지 진단 — url=%s title=%s ready=%s "
            "inputs=%s password=%s iframes=%s",
            url,
            title or "(empty)",
            ready or "?",
            n_input,
            n_password,
            n_iframe,
        )
    except Exception:
        logging.exception("로그인 페이지 진단 로깅 중 오류")


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
    return peek_attendance_snapshot(driver)[0]


def peek_attendance_snapshot(
    driver: webdriver.Chrome,
) -> tuple[AttendanceUiState, dict]:
    if not wait_for_attendance_url(driver):
        return "unknown", {}
    snap = _read_attendance_snapshot(driver)
    return _ui_state_from_snapshot(snap), snap


def _clock_text_to_datetime(day: date, clock: Optional[str]) -> Optional[datetime]:
    if not clock:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(clock, fmt).time()
        except ValueError:
            continue
        return datetime.combine(day, parsed)
    return None


def sync_local_attendance_from_server(
    ui_state: AttendanceUiState,
    snap: Optional[dict] = None,
    *,
    today: Optional[date] = None,
) -> None:
    """서버 UI에 이미 있는 출퇴근을 로컬 상태 파일에 맞춘다."""
    day = today or date.today()
    snapshot = snap or {}
    in_at = _clock_text_to_datetime(day, snapshot.get("in_time"))
    out_at = _clock_text_to_datetime(day, snapshot.get("out_time"))

    if ui_state in ("checked_in", "checked_out"):
        if load_last_check_in_date() != day:
            save_check_in_date(day, now=in_at)
            logging.info(
                "서버에 이미 출근됨 — 로컬 상태 동기화%s",
                f" time={snapshot.get('in_time')}" if snapshot.get("in_time") else "",
            )
    if ui_state == "checked_out" and load_last_check_out_date() != day:
        save_check_out_date(day, now=out_at)
        logging.info(
            "서버에 이미 퇴근됨 — 로컬 상태 동기화%s",
            f" time={snapshot.get('out_time')}" if snapshot.get("out_time") else "",
        )


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


def click_check_in_button(
    driver: webdriver.Chrome, *, force: bool = False
) -> bool:
    today = date.today()
    if force:
        if load_last_check_in_date() == today:
            logging.info("오늘 이미 출근 처리됨 — 수동 출근 생략")
            return False
    elif not should_attempt_check_in(today):
        return False

    if not _click_labeled_button(
        driver, xpaths=CHECK_IN_BUTTON_XPATHS, label="출근하기"
    ):
        ui_state, snap = peek_attendance_snapshot(driver)
        sync_local_attendance_from_server(ui_state, snap, today=today)
        if ui_state in ("checked_in", "checked_out"):
            logging.info("출근하기 버튼 없음 — 서버에 이미 처리됨, 로컬 동기화")
            return True
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
) -> LoginOutcome:
    """로그인 또는 기존 세션 확인.

    Returns:
        ok: 근태 화면 접근 가능
        auth: 폼 제출 후 로그인 유지 — 자격증명 문제로 재시도 중단
        network: 페이지/폼을 못 불러옴 — 재시도 대상
    """
    if not _wait_for_login_surface(driver, LOGIN_FORM_WAIT_SEC):
        _log_login_page_debug(driver)
        logging.warning(
            "페이지 로딩 시간 초과: %s — 새로고침 후 재시도",
            driver.current_url,
        )
        try:
            driver.refresh()
        except WebDriverException:
            logging.warning("로그인 페이지 새로고침 실패")
            return "network"
        if not _wait_for_login_surface(driver, LOGIN_FORM_RETRY_WAIT_SEC):
            _log_login_page_debug(driver)
            logging.warning("페이지 로딩 시간 초과: %s", driver.current_url)
            return "network"

    time.sleep(0.5)
    if (
        _is_attendance_url(driver)
        and _find_password_input(driver, visible_only=False) is None
    ):
        logging.info("이미 로그인된 세션 — 로그인 생략")
        return "ok"

    password_input = _find_password_input(driver, visible_only=True)
    username_input = _find_username_input(driver, visible_only=True)
    if password_input is None or username_input is None:
        hidden_password = _find_password_input(driver, visible_only=False)
        hidden_username = _find_username_input(driver, visible_only=False)
        if hidden_password is not None and hidden_username is not None:
            logging.warning(
                "로그인 입력란이 숨김 상태 — headless 표시 폴백 사용"
            )
            password_input = hidden_password
            username_input = hidden_username

    if password_input is None or username_input is None:
        logging.error("로그인 입력란을 찾지 못함 (url=%s)", driver.current_url)
        _log_login_page_debug(driver)
        return "network"

    logging.info("로그인 폼 감지 — 계정 정보 입력 시작")
    if username is None or password is None:
        try:
            username, password = load_login_credentials()
        except Exception:
            logging.exception("로그인 설정 로드 실패")
            return "auth"

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
        _log_login_page_debug(driver)
        return "network"

    login_button.click()
    logging.info("로그인 버튼 클릭")

    try:
        WebDriverWait(driver, ATTENDANCE_PAGE_WAIT_SEC).until(
            lambda d: "/login" not in (d.current_url or "")
            and _find_password_input(d, visible_only=False) is None
        )
        logging.info("자동 로그인 성공: %s", driver.current_url)
        return "ok"
    except TimeoutException:
        url = driver.current_url or ""
        _log_login_page_debug(driver)
        logging.warning("로그인 후 페이지 전환 확인 실패 (url=%s)", url)
        if "/login" in url:
            return "auth"
        return "network"


def _record_login_failure(
    action: Literal["check_in", "check_out"],
    outcome: LoginOutcome,
) -> None:
    if outcome == "auth":
        record_failure(action, "auth", "로그인에 실패했습니다")
        notify_attendance_failure(
            title="로그인 실패",
            message="아이디/비밀번호를 확인하세요. 자동 재시도를 중단합니다.",
        )
        return
    record_failure(action, "network", "로그인 페이지를 확인하지 못했습니다")
    notify_attendance_failure(
        title="로그인 페이지 오류",
        message="근태 로그인 화면을 불러오지 못했습니다. 잠시 후 재시도합니다.",
    )


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


def _run_with_driver(worker, *, headed: bool = False) -> None:
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
        driver = _create_driver(chrome, headed=headed)
        _active_drivers.append(driver)
        _goto_url(driver, attendance_url)
        worker(driver)
        if headed:
            time.sleep(HEADED_RESULT_HOLD_SEC)
    except ValueError:
        logging.exception("근태 URL/설정 로드 실패")
    except WebDriverException as exc:
        logging.exception("근태 작업 중 WebDriver 오류")
        action = getattr(worker, "_attendance_action", None)
        if action in ("check_in", "check_out"):
            record_failure(
                action,
                "network",
                f"WebDriver: {_webdriver_error_summary(exc)}",
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


def _auto_login_worker(
    _chrome: Path | None = None,
    *,
    headed: bool = False,
    force: bool = False,
) -> None:
    def _job(driver: webdriver.Chrome) -> None:
        outcome = login_if_needed(driver)
        if outcome != "ok":
            _record_login_failure("check_in", outcome)
            return
        ui_state, snap = peek_attendance_snapshot(driver)
        logging.info("서버 근태 UI 상태: %s", ui_state)
        sync_local_attendance_from_server(ui_state, snap)
        if ui_state in ("checked_in", "checked_out"):
            logging.info("서버에 오늘 출근이 이미 있음 — 출근하기 생략")
            return
        logging.info("근태 화면 전환 대기 후 출근하기 진행")
        click_check_in_button(driver, force=force)

    _job._attendance_action = "check_in"  # type: ignore[attr-defined]
    _run_with_driver(_job, headed=headed)


def _sync_attendance_worker(_chrome: Path | None = None) -> None:
    """클릭 없이 서버 출퇴근만 읽어 로컬에 반영."""

    def _job(driver: webdriver.Chrome) -> None:
        if login_if_needed(driver) != "ok":
            logging.warning("근태 상태 동기화 — 로그인 실패")
            return
        ui_state, snap = peek_attendance_snapshot(driver)
        logging.info("근태 상태 동기화 — 서버 UI: %s", ui_state)
        sync_local_attendance_from_server(ui_state, snap)
        if ui_state == "not_checked_in":
            logging.info("근태 상태 동기화 — 서버 미출근, 로컬 유지")
        elif ui_state == "unknown":
            logging.warning("근태 상태 동기화 — 서버 상태를 판별하지 못함")

    _run_with_driver(_job)


def _auto_checkout_worker(_chrome: Path | None = None) -> None:
    def _job(driver: webdriver.Chrome) -> None:
        outcome = login_if_needed(driver)
        if outcome != "ok":
            _record_login_failure("check_out", outcome)
            return

        logging.info("근태 화면 전환 후 서버 상태 peek")
        ui_state, snap = peek_attendance_snapshot(driver)
        logging.info("서버 근태 UI 상태: %s", ui_state)
        sync_local_attendance_from_server(ui_state, snap)

        if ui_state == "checked_out":
            logging.info("서버에 이미 퇴근됨 — 퇴근하기 생략")
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


def open_attendance_page(*, headed: bool = False, force: bool = False) -> bool:
    if not has_app_setup():
        logging.warning("앱 설정 없음 — 웹창 실행 생략")
        return False

    if not force:
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
        kwargs={"headed": headed, "force": force},
        name="auto-login",
        daemon=True,
    ).start()
    logging.info(
        "근태 페이지 자동 로그인 시작%s: %s",
        " (수동·창 표시)" if headed or force else "",
        attendance_url,
    )
    return True


def open_attendance_sync() -> bool:
    """서버 근태 화면만 읽어 로컬 출퇴근 상태를 맞춘다. 버튼 클릭 없음."""
    if not has_app_setup():
        logging.warning("앱 설정 없음 — 근태 상태 동기화 생략")
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
        target=_sync_attendance_worker,
        name="attendance-sync",
        daemon=True,
    ).start()
    logging.info("근태 상태 동기화 시작: %s", attendance_url)
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
        driver = _create_driver(chrome)
        _active_drivers.append(driver)
        _goto_url(driver, target_url)
        outcome = login_if_needed(driver, username=username, password=password)
        if outcome != "ok":
            if outcome == "auth":
                return False, "로그인에 실패했습니다. 아이디/비밀번호를 확인하세요."
            return (
                False,
                "로그인 페이지를 불러오지 못했습니다. 네트워크를 확인한 뒤 다시 시도하세요.",
            )
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