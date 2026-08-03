"""트레이 아이콘 및 메인 GUI."""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import tkinter as tk
from datetime import date, datetime, time as dt_time
from pathlib import Path
from tkinter import ttk
from typing import Optional

import pystray
from PIL import Image, ImageDraw

from startofwork.attendance_state import (
    get_check_in_status_text,
    get_check_out_status_text,
    get_monitor_attendance_snapshot,
    get_tray_status_text,
    is_attempt_allowed,
    is_auth_failure_blocking,
    is_retry_due,
)
from startofwork.browser import (
    consume_checkout_rearm,
    is_checkout_job_running,
    open_attendance_page,
    open_checkout_page,
    verify_login_credentials,
)
from startofwork.config import (
    has_app_setup,
    is_missing_attendance_url,
    is_missing_credentials,
    load_active_hours,
    load_auto_checkout_settings,
    load_update_check_enabled,
    normalize_attendance_url,
    parse_hhmm,
    save_active_hours,
    save_app_setup,
    save_auto_checkout_settings,
)
from startofwork.constants import (
    APP_TITLE,
    APP_VERSION,
    CHECK_INTERVAL_IDLE_MS,
    UPDATE_CHECK_TIME,
    CHECK_INTERVAL_MS,
    CHECK_INTERVAL_QUIET_MS,
    DEFAULT_ACTIVE_END_TIME,
    DEFAULT_ACTIVE_START_TIME,
    DEFAULT_AUTO_CHECKOUT_TIME,
    GWL_STYLE,
    POLL_BOUNDARY_WINDOW_SEC,
    SWP_FRAMECHANGED,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_NOZORDER,
    WS_MAXIMIZEBOX,
)
from startofwork.holidays import get_non_workday_reason, is_holiday_api_retry_due
from startofwork.lock_state import get_windows_lock_state
from startofwork.notifications import set_notification_handler
from startofwork.paths import APP_ICON_FILE
from startofwork.rules import is_within_active_hours, should_attempt_check_out, should_open_browser
from startofwork.updater import (
    ReleaseInfo,
    UpdateError,
    check_for_update,
    launch_standalone_updater,
)


def _seconds_until_time(now: datetime, target: dt_time) -> float:
    return (datetime.combine(now.date(), target) - now).total_seconds()


def _is_near_time(
    now: datetime,
    target: dt_time,
    *,
    window_sec: float = POLL_BOUNDARY_WINDOW_SEC,
) -> bool:
    """target 시각까지 window_sec 이내(진입 직전)이면 True."""
    delta = _seconds_until_time(now, target)
    return 0 <= delta <= window_sec


def next_poll_interval_ms(
    now: datetime,
    *,
    lock_state: Optional[bool],
    within: bool,
    checkout_enabled: bool,
    checkout_time: dt_time,
    checkout_triggered_date: Optional[date],
    non_workday_reason: Optional[str],
    last_check_in: Optional[date],
    last_check_out: Optional[date],
    active_start: dt_time,
    update_check_enabled: bool = False,
    checkout_retry_due: bool = False,
) -> int:
    """폴링 주기(ms). 출근/퇴근 임박은 짧게, 한산 구간은 길게."""
    today = now.date()
    checked_in = last_check_in == today
    checked_out = last_check_out == today
    pending_check_in = non_workday_reason is None and not checked_in
    # 로컬 출근 없어도 자동 퇴근 후보(서버 peek) — 미퇴근이면 pending
    pending_check_out = (
        checkout_enabled
        and not checked_out
        and (
            checkout_triggered_date != today
            or checkout_retry_due
        )
    )

    if pending_check_in and (
        within or _is_near_time(now, active_start) or lock_state is True
    ):
        return CHECK_INTERVAL_MS

    if pending_check_out and (
        now.time() >= checkout_time or _is_near_time(now, checkout_time)
    ):
        return CHECK_INTERVAL_MS

    # 새벽 1시 업데이트 확인 직전은 짧게 폴링해 시각을 놓치지 않음
    if update_check_enabled and _is_near_time(now, UPDATE_CHECK_TIME):
        return CHECK_INTERVAL_MS

    if pending_check_in or pending_check_out:
        return CHECK_INTERVAL_IDLE_MS

    return CHECK_INTERVAL_QUIET_MS

def create_tray_image() -> Image.Image:
    """앱 아이콘(.ico)을 트레이용 이미지로 로드"""
    try:
        if APP_ICON_FILE.is_file():
            with Image.open(APP_ICON_FILE) as icon:
                frames: list[Image.Image] = []
                try:
                    index = 0
                    while True:
                        icon.seek(index)
                        frames.append(icon.copy().convert("RGBA"))
                        index += 1
                except EOFError:
                    pass

                if frames:
                    image = max(frames, key=lambda frame: frame.size[0] * frame.size[1])
                else:
                    image = icon.convert("RGBA")
                return image.resize((64, 64), Image.Resampling.LANCZOS)
    except Exception:
        logging.exception("앱 아이콘 로드 실패 — 기본 트레이 이미지 사용")

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, size - 5, size - 5), fill=(33, 110, 57, 255))
    draw.rectangle((22, 18, 42, 46), fill=(234, 246, 238, 255))
    return image


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class LockStateMonitor(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_TITLE)
        self.geometry("520x400")
        self.minsize(500, 360)
        self.resizable(True, True)
        self._apply_window_icon()

        self.current_state: Optional[bool] = None
        self.last_changed_at: Optional[datetime] = None
        self.after_id: Optional[str] = None
        self._tray_icon: Optional[pystray.Icon] = None
        self._tray_thread: Optional[threading.Thread] = None
        self._hiding_to_tray = False
        self._normal_geometry = "520x400"
        self._restoring_from_maximize = False
        self._holiday_info_date: Optional[date] = None
        self._holiday_info_text = "확인 중"
        self._check_in_status_text = "확인 중"
        self._check_out_status_text = "확인 중"
        self._attendance_summary_text = "확인 중"
        self._checkout_triggered_date: Optional[date] = None
        self._login_setup_dialog: Optional[tk.Toplevel] = None
        self._login_verifying = False
        self._update_dialog: Optional[tk.Toplevel] = None
        self._update_busy = False
        self._pending_update: Optional[ReleaseInfo] = None
        self._startup_update_check_done = False
        # 매일 새벽 1시 업데이트 확인 (시각 진입 감지)
        self._was_past_update_check_time: Optional[bool] = None
        self._daily_update_check_date: Optional[date] = None
        # 부팅/로그인 직후 출근은 프로세스당 1회만 시도
        self._startup_check_in_attempted = False
        # 업무시간 시작 시각 진입 감지용 (날짜당 1회)
        self._was_within_active_hours: Optional[bool] = None
        self._active_start_check_in_date: Optional[date] = None
        self._last_ui_lock_state: Optional[bool] = None
        self._last_ui_within_hours: Optional[bool] = None
        self._last_changed_label: Optional[str] = None
        self._holiday_refresh_running = False
        self._last_monitor_date: Optional[date] = None

        active_start, active_end = load_active_hours()
        self.active_start_hour_var = tk.StringVar(value=f"{active_start.hour:02d}")
        self.active_start_minute_var = tk.StringVar(
            value=f"{active_start.minute:02d}"
        )
        self.active_end_hour_var = tk.StringVar(value=f"{active_end.hour:02d}")
        self.active_end_minute_var = tk.StringVar(value=f"{active_end.minute:02d}")

        enabled, checkout_time = load_auto_checkout_settings()
        self.auto_checkout_enabled = tk.BooleanVar(value=enabled)
        self.checkout_hour_var = tk.StringVar(value=f"{checkout_time.hour:02d}")
        self.checkout_minute_var = tk.StringVar(value=f"{checkout_time.minute:02d}")

        self._configure_styles()
        self._build_ui()
        self.after_idle(self._disable_maximize_button)
        set_notification_handler(self._dispatch_notification)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Unmap>", self._on_unmap)
        self.bind("<Map>", self._on_map)
        self.bind("<Configure>", self._on_configure)

        logging.info("프로그램 시작")
        # 캐시로 즉시 표시 후 Open API는 백그라운드 갱신 (메인 스레드 블로킹 방지)
        self._refresh_holidays_and_display(
            force=False, reason="프로그램 시작(캐시)", cache_only=True
        )
        self._schedule_holiday_refresh(force=True, reason="프로그램 시작(백그라운드)")
        self._update_check_in_display(datetime.now())
        self._update_check_out_display(datetime.now())
        self._update_monitor()

        if has_app_setup():
            # 시작 시 트레이로 최소화 (창 깜빡임 방지)
            self.withdraw()
            self.after_idle(self._minimize_to_tray)
            # 잠금→해제 없이 부팅/로그인만 한 경우에도 오늘 미출근이면 1회 시도
            self.after(2000, self._try_startup_check_in)
            self.after(5000, self._try_startup_update_check)
        else:
            logging.info("앱 설정 없음 — GUI에서 근태 URL·계정 입력 요청")
            self.after_idle(self._prompt_login_setup)

    def _prompt_login_setup(self) -> None:
        """최초 설정: ① 근태 URL → ② 아이디/비번 → 검증 후 저장."""
        if has_app_setup():
            return
        if self._login_setup_dialog is not None and self._login_setup_dialog.winfo_exists():
            self._login_setup_dialog.lift()
            self._login_setup_dialog.focus_force()
            return

        self.deiconify()
        self.lift()
        self.message_label.configure(
            text="최초 설정 — 근태 페이지 주소와 로그인 정보를 입력하세요"
        )

        dialog = tk.Toplevel(self)
        self._login_setup_dialog = dialog
        dialog.title("최초 설정")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        try:
            if APP_ICON_FILE.is_file():
                dialog.iconbitmap(default=str(APP_ICON_FILE))
        except tk.TclError:
            pass

        frame = ttk.Frame(dialog, padding=(20, 18, 20, 18))
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        step_var = tk.StringVar(value="1/2 근태 페이지 주소")
        ttk.Label(frame, textvariable=step_var, style="Info.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 12)
        )

        # --- Step 1: URL ---
        step1 = ttk.Frame(frame)
        step1.grid(row=1, column=0, columnspan=2, sticky="ew")
        step1.columnconfigure(1, weight=1)

        ttk.Label(
            step1,
            text="다우오피스 근태 페이지 URL을 입력하세요",
            style="Info.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(step1, text="근태 URL", style="Info.TLabel").grid(
            row=1, column=0, sticky="w", pady=6, padx=(0, 12)
        )
        url_var = tk.StringVar()
        url_entry = ttk.Entry(step1, textvariable=url_var, width=42)
        url_entry.grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(
            step1,
            text="예: https://회사명.daouoffice.com/ehr/app/attend/my-attendance-status",
            style="Caption.TLabel",
            wraplength=420,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # --- Step 2: credentials ---
        step2 = ttk.Frame(frame)
        step2.columnconfigure(1, weight=1)

        ttk.Label(
            step2,
            text="다우오피스 로그인 정보를 입력하세요",
            style="Info.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(step2, text="아이디", style="Info.TLabel").grid(
            row=1, column=0, sticky="w", pady=6, padx=(0, 12)
        )
        username_var = tk.StringVar()
        username_entry = ttk.Entry(step2, textvariable=username_var, width=28)
        username_entry.grid(row=1, column=1, sticky="ew", pady=6)

        ttk.Label(step2, text="비밀번호", style="Info.TLabel").grid(
            row=2, column=0, sticky="w", pady=6, padx=(0, 12)
        )
        password_var = tk.StringVar()
        password_entry = ttk.Entry(
            step2, textvariable=password_var, width=28, show="*"
        )
        password_entry.grid(row=2, column=1, sticky="ew", pady=6)

        status_var = tk.StringVar(
            value="다음을 누른 뒤 아이디/비밀번호를 입력하고 확인하면 검증·저장합니다."
        )
        status_label = ttk.Label(
            frame,
            textvariable=status_var,
            style="Caption.TLabel",
            wraplength=420,
            justify="left",
        )
        status_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 12))

        button_row = ttk.Frame(frame)
        button_row.grid(row=3, column=0, columnspan=2, sticky="e")

        back_button = ttk.Button(button_row, text="이전")
        next_button = ttk.Button(button_row, text="다음")
        verify_button = ttk.Button(button_row, text="확인 및 저장")

        current_step = {"n": 1}
        pending_url = {"value": ""}

        def _show_step(step: int) -> None:
            current_step["n"] = step
            if step == 1:
                step_var.set("1/2 근태 페이지 주소")
                step2.grid_remove()
                step1.grid(row=1, column=0, columnspan=2, sticky="ew")
                back_button.pack_forget()
                verify_button.pack_forget()
                next_button.pack(side="right")
                status_var.set("근태 페이지 전체 주소를 입력한 뒤 다음을 누르세요.")
                url_entry.focus_set()
            else:
                step_var.set("2/2 로그인 정보")
                step1.grid_remove()
                step2.grid(row=1, column=0, columnspan=2, sticky="ew")
                next_button.pack_forget()
                back_button.pack(side="right", padx=(0, 8))
                verify_button.pack(side="right")
                status_var.set(
                    "확인 버튼을 누르면 입력한 URL로 로그인·근태 페이지를 검증합니다."
                )
                username_entry.focus_set()

        def _set_busy(busy: bool) -> None:
            self._login_verifying = busy
            state = "disabled" if busy else "normal"
            url_entry.configure(state=state)
            username_entry.configure(state=state)
            password_entry.configure(state=state)
            next_button.configure(state=state)
            back_button.configure(state=state)
            verify_button.configure(state=state)

        def _on_next() -> None:
            if self._login_verifying:
                return
            url = normalize_attendance_url(url_var.get())
            if is_missing_attendance_url(url):
                status_var.set("근태 URL을 http:// 또는 https:// 로 시작하는 주소로 입력하세요.")
                url_entry.focus_set()
                return
            pending_url["value"] = url
            _show_step(2)

        def _on_back() -> None:
            if self._login_verifying:
                return
            _show_step(1)

        def _on_verify() -> None:
            if self._login_verifying:
                return
            attendance_url = pending_url["value"] or normalize_attendance_url(
                url_var.get()
            )
            username = username_var.get().strip()
            password = password_var.get().strip()
            if is_missing_attendance_url(attendance_url):
                status_var.set("근태 URL이 없습니다. 이전 단계에서 다시 입력하세요.")
                _show_step(1)
                return
            if is_missing_credentials(username, password):
                status_var.set("아이디와 비밀번호를 모두 입력하세요.")
                return

            _set_busy(True)
            status_var.set("로그인 확인 중… (잠시만 기다려 주세요)")
            dialog.update_idletasks()

            def _worker() -> None:
                ok, message = verify_login_credentials(
                    username,
                    password,
                    attendance_url=attendance_url,
                )
                self.after(
                    0,
                    lambda: _on_verify_done(
                        ok, message, attendance_url, username, password
                    ),
                )

            threading.Thread(
                target=_worker,
                name="login-verify",
                daemon=True,
            ).start()

        def _on_verify_done(
            ok: bool,
            message: str,
            attendance_url: str,
            username: str,
            password: str,
        ) -> None:
            if not dialog.winfo_exists():
                self._login_verifying = False
                return

            if not ok:
                _set_busy(False)
                status_var.set(message)
                self.message_label.configure(text=message)
                password_entry.focus_set()
                return

            try:
                save_app_setup(attendance_url, username, password)
            except Exception:
                logging.exception("앱 설정 저장 실패")
                _set_busy(False)
                status_var.set("로그인은 확인되었지만 설정 저장에 실패했습니다.")
                return

            status_var.set(message)
            self.message_label.configure(text="최초 설정이 저장되었습니다")
            logging.info("GUI 최초 설정 완료")
            dialog.grab_release()
            dialog.destroy()
            self._login_setup_dialog = None
            self._login_verifying = False
            self.after(300, self._minimize_to_tray)
            self.after(2000, self._try_startup_check_in)

        next_button.configure(command=_on_next)
        back_button.configure(command=_on_back)
        verify_button.configure(command=_on_verify)

        def _on_dialog_close() -> None:
            if self._login_verifying:
                status_var.set("로그인 확인 중입니다. 잠시만 기다려 주세요.")
                return
            logging.info("최초 설정 취소 — 프로그램 종료")
            dialog.grab_release()
            dialog.destroy()
            self._login_setup_dialog = None
            self._quit_application()

        dialog.protocol("WM_DELETE_WINDOW", _on_dialog_close)

        def _on_return(_event=None) -> None:
            if current_step["n"] == 1:
                _on_next()
            else:
                _on_verify()

        dialog.bind("<Return>", _on_return)

        _show_step(1)
        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        parent_w = max(self.winfo_width(), 1)
        parent_h = max(self.winfo_height(), 1)
        x = parent_x + max((parent_w - width) // 2, 0)
        y = parent_y + max((parent_h - height) // 2, 0)
        dialog.geometry(f"+{x}+{y}")

        url_entry.focus_set()
    def _apply_window_icon(self) -> None:
        """윈도우 창/작업표시줄 아이콘 설정"""
        if not APP_ICON_FILE.is_file():
            logging.warning("앱 아이콘 파일 없음: %s", APP_ICON_FILE)
            return
        try:
            self.iconbitmap(default=str(APP_ICON_FILE))
        except tk.TclError:
            try:
                self.iconbitmap(str(APP_ICON_FILE))
            except tk.TclError:
                logging.exception("윈도우 아이콘 설정 실패")

    def _get_toplevel_hwnd(self) -> int:
        """Tk 최상위 프레임 HWND 반환"""
        self.update_idletasks()
        frame = self.wm_frame()
        if frame:
            return int(frame, 16)
        return int(self.winfo_id())

    def _disable_maximize_button(self) -> None:
        """최대화 버튼을 비활성화해 현재 창 크기를 유지"""
        try:
            hwnd = self._get_toplevel_hwnd()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            if style & WS_MAXIMIZEBOX:
                style &= ~WS_MAXIMIZEBOX
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style)
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    None,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED,
                )
        except Exception:
            logging.exception("최대화 버튼 비활성화 실패")

    def _on_map(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self.after_idle(self._disable_maximize_button)
        self.after_idle(self._restore_if_maximized)

    def _on_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        self._restore_if_maximized()
        if self._restoring_from_maximize:
            return
        try:
            if self.state() == "normal":
                self._normal_geometry = self.geometry()
        except tk.TclError:
            pass

    def _restore_if_maximized(self) -> None:
        """최대화되면 직전 일반 창 크기로 되돌림"""
        if self._restoring_from_maximize:
            return
        try:
            if self.state() != "zoomed":
                return
        except tk.TclError:
            return

        self._restoring_from_maximize = True
        try:
            self.state("normal")
            self.geometry(self._normal_geometry)
            self.after_idle(self._disable_maximize_button)
        finally:
            self.after(50, self._clear_maximize_restore_flag)

    def _clear_maximize_restore_flag(self) -> None:
        self._restoring_from_maximize = False

    def _configure_styles(self) -> None:
        style = ttk.Style(self)

        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        style.configure("Title.TLabel", font=("맑은 고딕", 16, "bold"))
        style.configure("Caption.TLabel", font=("맑은 고딕", 10))
        style.configure("Info.TLabel", font=("맑은 고딕", 11))
        style.configure("Info.TCheckbutton", font=("맑은 고딕", 11))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=(22, 14, 22, 14))
        outer.pack(fill="x", expand=False)

        self.state_frame = tk.Frame(
            outer,
            background="#ECEFF1",
            highlightthickness=1,
            highlightbackground="#CFD8DC",
            height=64,
        )
        self.state_frame.pack(fill="x", padx=28, pady=(0, 10))
        self.state_frame.pack_propagate(False)

        self.state_label = tk.Label(
            self.state_frame,
            text="확인 중",
            font=("맑은 고딕", 22, "bold"),
            foreground="#455A64",
            background="#ECEFF1",
            anchor="center",
            justify="center",
        )
        self.state_label.place(relx=0.5, rely=0.5, anchor="center")

        details = ttk.Frame(outer)
        details.pack(fill="x", padx=30, pady=(0, 4))

        ttk.Label(
            details,
            text="마지막 상태 변경",
            style="Info.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=5)

        self.changed_label = ttk.Label(
            details,
            text="-",
            style="Info.TLabel",
        )
        self.changed_label.grid(row=0, column=1, sticky="e", pady=5)

        ttk.Label(
            details,
            text="공휴일 유무",
            style="Info.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=5)

        self.holiday_label = ttk.Label(
            details,
            text="확인 중",
            style="Info.TLabel",
        )
        self.holiday_label.grid(row=1, column=1, sticky="e", pady=5)

        ttk.Label(
            details,
            text="출근체크",
            style="Info.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=5)

        self.check_in_label = ttk.Label(
            details,
            text="확인 중",
            style="Info.TLabel",
        )
        self.check_in_label.grid(row=2, column=1, sticky="e", pady=5)

        ttk.Label(
            details,
            text="퇴근체크",
            style="Info.TLabel",
        ).grid(row=3, column=0, sticky="w", pady=5)

        self.check_out_label = ttk.Label(
            details,
            text="확인 중",
            style="Info.TLabel",
        )
        self.check_out_label.grid(row=3, column=1, sticky="e", pady=5)

        details.columnconfigure(1, weight=1)

        hours_frame = ttk.Frame(outer)
        hours_frame.pack(fill="x", padx=30, pady=(8, 0))

        ttk.Label(hours_frame, text="업무시간", style="Info.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        hours_box = ttk.Frame(hours_frame)
        hours_box.grid(row=0, column=1, sticky="e")

        self.active_start_hour_spin = ttk.Spinbox(
            hours_box,
            from_=0,
            to=23,
            width=3,
            textvariable=self.active_start_hour_var,
            format="%02.0f",
            command=self._on_active_hours_changed,
        )
        self.active_start_hour_spin.pack(side="left")
        ttk.Label(hours_box, text=":", style="Info.TLabel").pack(side="left", padx=2)
        self.active_start_minute_spin = ttk.Spinbox(
            hours_box,
            from_=0,
            to=59,
            width=3,
            textvariable=self.active_start_minute_var,
            format="%02.0f",
            command=self._on_active_hours_changed,
        )
        self.active_start_minute_spin.pack(side="left")
        ttk.Label(hours_box, text="~", style="Info.TLabel").pack(
            side="left", padx=(6, 6)
        )
        self.active_end_hour_spin = ttk.Spinbox(
            hours_box,
            from_=0,
            to=23,
            width=3,
            textvariable=self.active_end_hour_var,
            format="%02.0f",
            command=self._on_active_hours_changed,
        )
        self.active_end_hour_spin.pack(side="left")
        ttk.Label(hours_box, text=":", style="Info.TLabel").pack(side="left", padx=2)
        self.active_end_minute_spin = ttk.Spinbox(
            hours_box,
            from_=0,
            to=59,
            width=3,
            textvariable=self.active_end_minute_var,
            format="%02.0f",
            command=self._on_active_hours_changed,
        )
        self.active_end_minute_spin.pack(side="left")
        hours_frame.columnconfigure(0, weight=1)

        self._active_hours_save_after_id: Optional[str] = None
        self._suppress_active_hours_save = False
        self._checkout_save_after_id: Optional[str] = None
        self._suppress_checkout_save = False
        for spin in (
            self.active_start_hour_spin,
            self.active_start_minute_spin,
            self.active_end_hour_spin,
            self.active_end_minute_spin,
        ):
            spin.bind("<FocusOut>", lambda _e: self._on_active_hours_changed())
        for var in (
            self.active_start_hour_var,
            self.active_start_minute_var,
            self.active_end_hour_var,
            self.active_end_minute_var,
        ):
            var.trace_add("write", lambda *_: self._schedule_active_hours_save())

        checkout_frame = ttk.Frame(outer)
        checkout_frame.pack(fill="x", padx=30, pady=(8, 0))

        self.auto_checkout_check = ttk.Checkbutton(
            checkout_frame,
            text="자동 퇴근 활성화",
            variable=self.auto_checkout_enabled,
            command=self._on_auto_checkout_settings_changed,
            style="Info.TCheckbutton",
        )
        self.auto_checkout_check.grid(row=0, column=0, sticky="w")

        time_box = ttk.Frame(checkout_frame)
        time_box.grid(row=0, column=1, sticky="e")

        ttk.Label(time_box, text="퇴근 시각", style="Info.TLabel").pack(
            side="left", padx=(0, 6)
        )
        self.checkout_hour_spin = ttk.Spinbox(
            time_box,
            from_=0,
            to=23,
            width=3,
            textvariable=self.checkout_hour_var,
            format="%02.0f",
            command=self._on_auto_checkout_settings_changed,
        )
        self.checkout_hour_spin.pack(side="left")
        ttk.Label(time_box, text=":", style="Info.TLabel").pack(side="left", padx=2)
        self.checkout_minute_spin = ttk.Spinbox(
            time_box,
            from_=0,
            to=59,
            width=3,
            textvariable=self.checkout_minute_var,
            format="%02.0f",
            command=self._on_auto_checkout_settings_changed,
        )
        self.checkout_minute_spin.pack(side="left")

        checkout_frame.columnconfigure(0, weight=1)
        checkout_frame.columnconfigure(1, weight=0)

        self.checkout_hour_spin.bind(
            "<FocusOut>", lambda _e: self._on_auto_checkout_settings_changed()
        )
        self.checkout_minute_spin.bind(
            "<FocusOut>", lambda _e: self._on_auto_checkout_settings_changed()
        )
        self.checkout_hour_var.trace_add(
            "write", lambda *_: self._schedule_checkout_settings_save()
        )
        self.checkout_minute_var.trace_add(
            "write", lambda *_: self._schedule_checkout_settings_save()
        )

        start, end = load_active_hours()
        self.message_label = ttk.Label(
            outer,
            text=(
                f"{start.strftime('%H:%M')}~{end.strftime('%H:%M')} "
                "시작·업무시간 시작·잠금 해제 시 출근을 시도합니다"
            ),
            style="Caption.TLabel",
        )
        self.message_label.pack(anchor="center", pady=(8, 0))

        self.update_idletasks()
        fit_w = max(self.winfo_reqwidth(), 500)
        fit_h = max(self.winfo_reqheight(), 320)
        self.geometry(f"{fit_w}x{fit_h}")
        self.minsize(fit_w, fit_h)
        self._normal_geometry = f"{fit_w}x{fit_h}"

    def _parse_time_vars(
        self,
        hour_var: tk.StringVar,
        minute_var: tk.StringVar,
        default: dt_time,
    ) -> dt_time:
        return parse_hhmm(
            f"{hour_var.get().strip()}:{minute_var.get().strip()}",
            default,
        )

    def _get_selected_active_hours(self) -> tuple[dt_time, dt_time]:
        start = self._parse_time_vars(
            self.active_start_hour_var,
            self.active_start_minute_var,
            DEFAULT_ACTIVE_START_TIME,
        )
        end = self._parse_time_vars(
            self.active_end_hour_var,
            self.active_end_minute_var,
            DEFAULT_ACTIVE_END_TIME,
        )
        if start > end:
            start, end = DEFAULT_ACTIVE_START_TIME, DEFAULT_ACTIVE_END_TIME
        return start, end

    def _get_selected_checkout_time(self) -> dt_time:
        return self._parse_time_vars(
            self.checkout_hour_var,
            self.checkout_minute_var,
            DEFAULT_AUTO_CHECKOUT_TIME,
        )

    def _schedule_active_hours_save(self) -> None:
        if self._suppress_active_hours_save:
            return
        if self._active_hours_save_after_id is not None:
            try:
                self.after_cancel(self._active_hours_save_after_id)
            except tk.TclError:
                pass
        self._active_hours_save_after_id = self.after(
            400, self._on_active_hours_changed
        )

    def _set_time_var_silent(
        self,
        var: tk.StringVar,
        value: str,
        *,
        suppress_attr: str,
    ) -> None:
        if var.get() == value:
            return
        setattr(self, suppress_attr, True)
        try:
            var.set(value)
        finally:
            setattr(self, suppress_attr, False)

    def _on_active_hours_changed(self) -> None:
        start, end = self._get_selected_active_hours()
        self._set_time_var_silent(
            self.active_start_hour_var,
            f"{start.hour:02d}",
            suppress_attr="_suppress_active_hours_save",
        )
        self._set_time_var_silent(
            self.active_start_minute_var,
            f"{start.minute:02d}",
            suppress_attr="_suppress_active_hours_save",
        )
        self._set_time_var_silent(
            self.active_end_hour_var,
            f"{end.hour:02d}",
            suppress_attr="_suppress_active_hours_save",
        )
        self._set_time_var_silent(
            self.active_end_minute_var,
            f"{end.minute:02d}",
            suppress_attr="_suppress_active_hours_save",
        )
        save_active_hours(start, end)
        if self._is_ui_visible():
            self.message_label.configure(
                text=(
                    f"{start.strftime('%H:%M')}~{end.strftime('%H:%M')} "
                    "시작·업무시간 시작·잠금 해제 시 출근을 시도합니다"
                )
            )

    def _schedule_checkout_settings_save(self) -> None:
        if self._suppress_checkout_save:
            return
        if self._checkout_save_after_id is not None:
            try:
                self.after_cancel(self._checkout_save_after_id)
            except tk.TclError:
                pass
        self._checkout_save_after_id = self.after(
            400, self._on_auto_checkout_settings_changed
        )

    def _on_auto_checkout_settings_changed(self) -> None:
        enabled = bool(self.auto_checkout_enabled.get())
        checkout_time = self._get_selected_checkout_time()
        self._set_time_var_silent(
            self.checkout_hour_var,
            f"{checkout_time.hour:02d}",
            suppress_attr="_suppress_checkout_save",
        )
        self._set_time_var_silent(
            self.checkout_minute_var,
            f"{checkout_time.minute:02d}",
            suppress_attr="_suppress_checkout_save",
        )
        save_auto_checkout_settings(enabled, checkout_time)
        if datetime.now().time() < checkout_time:
            self._checkout_triggered_date = None

    def _maybe_run_auto_checkout(self, now: datetime) -> None:
        if not bool(self.auto_checkout_enabled.get()):
            return
        if is_checkout_job_running():
            return
        if consume_checkout_rearm():
            self._checkout_triggered_date = None
        if is_auth_failure_blocking("check_out", today=now.date()):
            return

        retry_due = is_retry_due("check_out", now=now)
        if self._checkout_triggered_date == now.date() and not retry_due:
            return

        checkout_time = self._get_selected_checkout_time()
        if not should_attempt_check_out(
            now.date(),
            checkout_time=checkout_time,
            now=now,
        ):
            return

        if not is_attempt_allowed("check_out", now=now):
            return

        self._checkout_triggered_date = now.date()
        if open_checkout_page():
            if self._is_ui_visible():
                self.message_label.configure(text="자동 퇴근을 시작했습니다")
        else:
            # 시작 실패 시 재시도 가능하도록 플래그 해제
            self._checkout_triggered_date = None
            if self._is_ui_visible():
                self.message_label.configure(text="자동 퇴근 시작 실패 — 로그 확인")

    def _update_check_out_display(self, now: datetime) -> None:
        self._apply_check_out_status(get_check_out_status_text(now.date()))

    def _apply_check_in_status(self, status: str) -> None:
        if status == self._check_in_status_text:
            return
        self._check_in_status_text = status
        if self._is_ui_visible():
            self.check_in_label.configure(text=status)
        self._refresh_attendance_summary()

    def _apply_check_out_status(self, status: str) -> None:
        if status == self._check_out_status_text:
            return
        self._check_out_status_text = status
        if self._is_ui_visible():
            self.check_out_label.configure(text=status)
        self._refresh_attendance_summary()

    def _refresh_attendance_summary(self, today: Optional[date] = None) -> None:
        """창 제목·트레이 툴팁용 출퇴근 요약 갱신 (퇴근 우선, 자정·공휴일 반영)."""
        summary = get_tray_status_text(today)
        if summary == self._attendance_summary_text:
            return
        self._attendance_summary_text = summary
        if self._is_ui_visible():
            self._apply_window_title()
        self._update_tray_title()

    def _apply_window_title(self) -> None:
        """윈도우 타이틀에 앱 이름과 출퇴근 요약 표시"""
        self.title(self._status_label_text())

    def _status_label_text(self) -> str:
        """창 제목·트레이 툴팁에 사용하는 텍스트"""
        return (
            f"{APP_TITLE} v{APP_VERSION} - {self._attendance_summary_text}"
        )

    def _try_startup_update_check(self) -> None:
        if self._startup_update_check_done:
            return
        self._startup_update_check_done = True
        if not load_update_check_enabled():
            logging.info("업데이트 확인 비활성화 — 시작 시 확인 생략")
            return
        self._run_update_check(source="auto")

    def _maybe_run_daily_update_check(self, now: datetime) -> None:
        """매일 UPDATE_CHECK_TIME(새벽 1시) 진입 시 1회 업데이트 확인."""
        past = now.time() >= UPDATE_CHECK_TIME
        previous = self._was_past_update_check_time
        self._was_past_update_check_time = past

        if previous is not False or not past:
            return
        if self._daily_update_check_date == now.date():
            return
        if not load_update_check_enabled():
            logging.info("업데이트 확인 비활성화 — 정기 확인 생략")
            return

        self._daily_update_check_date = now.date()
        logging.info(
            "정기 업데이트 확인 (%s)",
            UPDATE_CHECK_TIME.strftime("%H:%M"),
        )
        self._run_update_check(source="auto")

    def _tray_check_update(
        self, icon: Optional[pystray.Icon] = None, item=None
    ) -> None:
        self.after(0, lambda: self._run_update_check(source="tray"))

    def _run_update_check(self, *, source: str = "tray") -> None:
        """source: auto(시작/정기) | tray(트레이 메뉴)."""
        if self._update_busy:
            return

        def _worker() -> None:
            try:
                release, message = check_for_update()
            except UpdateError as exc:
                self.after(
                    0,
                    lambda: self._on_update_check_done(
                        None, str(exc), source=source
                    ),
                )
                return
            except Exception:
                logging.exception("업데이트 확인 실패")
                self.after(
                    0,
                    lambda: self._on_update_check_done(
                        None,
                        "업데이트 확인 중 오류가 발생했습니다.",
                        source=source,
                    ),
                )
                return
            self.after(
                0,
                lambda: self._on_update_check_done(
                    release, message, source=source
                ),
            )

        threading.Thread(
            target=_worker,
            name="update-check",
            daemon=True,
        ).start()

    def _on_update_check_done(
        self,
        release: Optional[ReleaseInfo],
        message: str,
        *,
        source: str,
    ) -> None:
        if release is not None:
            self._pending_update = release
            logging.info("업데이트 가능: %s", release.version)
            notice = f"새 버전 {release.version} 사용 가능"
            if source == "auto":
                self._dispatch_notification(
                    "업데이트 알림",
                    f"{notice} — 트레이에서 업데이트 확인",
                )
                return
            # 트레이: 알림으로 결과 표시 후 설치 대화상자
            self._dispatch_notification("업데이트 알림", notice)
            self._show_update_dialog(release, message)
            return

        self._pending_update = None
        logging.info("업데이트 확인: %s", message)
        if source == "auto":
            return
        # 트레이에서 확인한 경우 GUI를 띄우지 않고 알림만
        self._dispatch_notification("업데이트 확인", message)

    def _show_update_dialog(self, release: ReleaseInfo, message: str) -> None:
        if self._update_dialog is not None and self._update_dialog.winfo_exists():
            self._update_dialog.lift()
            self._update_dialog.focus_force()
            return

        self.deiconify()
        self.lift()

        dialog = tk.Toplevel(self)
        self._update_dialog = dialog
        dialog.title("업데이트")
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=(20, 18, 20, 18))
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=f"새 버전 {release.version} 사용 가능",
            style="Info.TLabel",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(
            frame,
            text=f"현재 버전: {APP_VERSION}",
            style="Caption.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 4))
        ttk.Label(
            frame,
            text=f"설치 파일: {release.asset_name}",
            style="Caption.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 10))

        status_var = tk.StringVar(
            value=f"{message} — 「업데이트 실행」을 누르면 업데이터가 다운로드·설치를 진행합니다."
        )
        ttk.Label(
            frame,
            textvariable=status_var,
            style="Caption.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(0, 12))

        button_row = ttk.Frame(frame)
        button_row.grid(row=4, column=0, columnspan=2, sticky="e")

        def _close_dialog() -> None:
            if self._update_busy:
                status_var.set("업데이터를 시작하는 중입니다. 잠시만 기다려 주세요.")
                return
            dialog.grab_release()
            dialog.destroy()
            self._update_dialog = None
            self._update_busy = False

        def _on_run_updater() -> None:
            if self._update_busy:
                return
            self._update_busy = True
            status_var.set("업데이터를 시작합니다…")
            dialog.update_idletasks()
            try:
                launch_standalone_updater(release, pid=os.getpid())
            except UpdateError as exc:
                self._update_busy = False
                status_var.set(str(exc))
                return
            except Exception:
                logging.exception("업데이터 시작 실패")
                self._update_busy = False
                status_var.set("업데이터 시작에 실패했습니다.")
                return

            dialog.grab_release()
            dialog.destroy()
            self._update_dialog = None
            self._update_busy = False
            self._pending_update = None
            # 메인은 업데이터가 다운로드 후 종료한다

        ttk.Button(
            button_row, text="업데이트 실행", command=_on_run_updater
        ).pack(side="right")
        ttk.Button(button_row, text="나중에", command=_close_dialog).pack(
            side="right", padx=(0, 8)
        )

        dialog.protocol("WM_DELETE_WINDOW", _close_dialog)
        frame.columnconfigure(0, weight=1)
        dialog.update_idletasks()
        dialog.geometry(
            f"+{self.winfo_rootx() + 40}+{self.winfo_rooty() + 40}"
        )

    def _update_tray_title(self) -> None:
        """트레이 아이콘 툴팁에 출퇴근 요약 반영"""
        icon = self._tray_icon
        if icon is None:
            return
        try:
            icon.title = self._status_label_text()
        except Exception:
            logging.exception("트레이 라벨 갱신 실패")

    def _dispatch_notification(self, title: str, message: str) -> None:
        """출근/퇴근 완료 알림 — 트레이 notify (PowerShell 없음)."""
        def _show() -> None:
            icon = self._tray_icon
            body = " ".join(message.split())
            if icon is not None:
                try:
                    icon.notify(body, title)
                    return
                except Exception:
                    logging.exception("트레이 알림 실패")
            if self._is_ui_visible():
                try:
                    self.message_label.configure(text=f"{title} — {body}")
                except Exception:
                    logging.exception("GUI 알림 문구 갱신 실패")

        try:
            self.after(0, _show)
        except Exception:
            _show()

    def _is_ui_visible(self) -> bool:
        try:
            return bool(self.winfo_viewable())
        except tk.TclError:
            return False

    def _update_check_in_display(self, now: datetime) -> None:
        """GUI·타이틀·트레이 라벨에 오늘 출근체크 여부 표시"""
        self._apply_check_in_status(get_check_in_status_text(now.date()))

    def _apply_holiday_labels(self, status_text: str) -> None:
        self._holiday_info_text = status_text
        if self._is_ui_visible():
            self.holiday_label.configure(text=status_text)

    def _holiday_status_text(self, reason_text: Optional[str]) -> str:
        if reason_text is None:
            return "아니오 (근무일)"
        return f"예 — {reason_text}"

    def _apply_holiday_refresh_result(
        self,
        today: date,
        status_text: str,
        *,
        reason: str,
    ) -> None:
        self._holiday_refresh_running = False
        self._holiday_info_date = today
        self._apply_holiday_labels(status_text)
        now = datetime.now()
        self._update_check_in_display(now)
        self._update_check_out_display(now)
        self._refresh_attendance_summary(today)
        logging.info("공휴일 GUI 갱신 완료 — 사유: %s — %s", reason, status_text)

    def _schedule_holiday_refresh(self, *, force: bool, reason: str) -> None:
        """공휴일 Open API 조회를 백그라운드에서 수행 후 UI에 반영."""
        if self._holiday_refresh_running:
            logging.info("공휴일 백그라운드 갱신 진행 중 — 생략 (%s)", reason)
            return
        self._holiday_refresh_running = True
        logging.info("공휴일 백그라운드 갱신 시작 — 사유: %s", reason)

        def worker() -> None:
            try:
                today = date.today()
                reason_text = get_non_workday_reason(today, force_refresh=force)
                status_text = self._holiday_status_text(reason_text)
            except Exception:
                logging.exception("공휴일 백그라운드 갱신 실패")
                self.after(0, self._clear_holiday_refresh_flag)
                return

            self.after(
                0,
                lambda t=today, s=status_text, r=reason: self._apply_holiday_refresh_result(
                    t, s, reason=r
                ),
            )

        threading.Thread(
            target=worker,
            name="holiday-refresh",
            daemon=True,
        ).start()

    def _clear_holiday_refresh_flag(self) -> None:
        self._holiday_refresh_running = False

    def _refresh_holidays_and_display(
        self,
        *,
        force: bool,
        reason: str,
        cache_only: bool = False,
    ) -> None:
        """공휴일 정보를 확인하고 GUI에 반영 (동기, 캐시 조회용)."""
        now = datetime.now()
        today = now.date()
        logging.info("공휴일 GUI 갱신 시작 — 사유: %s", reason)

        reason_text = get_non_workday_reason(
            today, force_refresh=force, cache_only=cache_only
        )
        status_text = self._holiday_status_text(reason_text)

        self._holiday_info_date = today
        self._apply_holiday_labels(status_text)
        self._update_check_in_display(now)
        logging.info("공휴일 GUI 갱신 완료 — %s", status_text)

    def _update_holiday_display(self, now: datetime) -> None:
        """자정 경과 또는 API 실패 후 08:00 재시도 시 공휴일 정보를 다시 확인·표시."""
        today = now.date()
        if self._holiday_info_date != today:
            # 캐시/주말 판정으로 즉시 갱신 후 API는 백그라운드
            self._refresh_holidays_and_display(
                force=False,
                reason="자정 경과(캐시)",
                cache_only=True,
            )
            self._schedule_holiday_refresh(
                force=True,
                reason="자정 경과(백그라운드)",
            )
            return

        if is_holiday_api_retry_due(now):
            self._schedule_holiday_refresh(
                force=True,
                reason="공휴일 API 재시도(08:00)",
            )

    def _set_state_display(
        self,
        state: Optional[bool],
        *,
        within_hours: Optional[bool] = None,
        force: bool = False,
    ) -> None:
        if within_hours is None:
            within_hours = is_within_active_hours()

        if (
            not force
            and state == self._last_ui_lock_state
            and within_hours == self._last_ui_within_hours
        ):
            return

        self._last_ui_lock_state = state
        self._last_ui_within_hours = within_hours

        if state is True:
            text = "잠금 상태"
            foreground = "#9B2C2C"
            background = "#FDECEC"
            border = "#E9B8B8"
            message = "잠금 해제 대기 중"

        elif state is False:
            text = "잠금 해제"
            foreground = "#216E39"
            background = "#EAF6EE"
            border = "#B7D9C1"
            if within_hours:
                message = "활성 시간대 — 시작·업무시간 시작·잠금 해제 시 출근 시도"
            else:
                message = "활성 시간대 외 — 업무시간 시작 시 출근 대기"

        else:
            text = "확인 불가"
            foreground = "#455A64"
            background = "#ECEFF1"
            border = "#CFD8DC"
            message = "상태 조회 실패 — 로그 파일 확인 필요"

        self.state_frame.configure(
            background=background,
            highlightbackground=border,
        )
        self.state_label.configure(
            text=text,
            foreground=foreground,
            background=background,
        )
        self.message_label.configure(text=message)

    def _trigger_check_in_if_allowed(
        self,
        now: datetime,
        *,
        trigger: str,
    ) -> Optional[str]:
        """조건 충족 시 출근 작업을 시작하고 GUI용 메시지를 반환."""
        if not has_app_setup():
            self.after(0, self._prompt_login_setup)
            return "최초 설정 필요 — 근태 URL과 아이디/비밀번호를 입력하세요"

        if is_auth_failure_blocking("check_in", today=now.date()):
            logging.info("%s 출근 생략 — 인증 실패로 재시도 중단", trigger)
            return "로그인 실패 — 설정에서 계정을 확인하세요"

        if not is_attempt_allowed("check_in", now=now):
            logging.info("%s 출근 생략 — 재시도 대기 중", trigger)
            return "출근 재시도 대기 중"

        allowed, reason = should_open_browser(now)
        if not allowed:
            logging.info(
                "%s 출근 생략 — 사유: %s (%s)",
                trigger,
                reason,
                now.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return f"{reason} — Chrome을 실행하지 않음"

        if open_attendance_page():
            logging.info("%s — 출근체크 시작", trigger)
            return f"{trigger} — 출근체크를 시작했습니다"
        return "Chrome 실행 실패 — 로그 파일 확인 필요"

    def _try_startup_check_in(self) -> None:
        """
        부팅/로그인 직후(프로세스 시작 시) 오늘 미출근이면 1회 출근 시도.
        잠금→해제 전환이 없어도 동작한다.
        """
        if self._startup_check_in_attempted:
            return
        self._startup_check_in_attempted = True

        if not has_app_setup():
            logging.info("시작 시 출근 생략 — 앱 설정 없음")
            return

        now = datetime.now()
        state = get_windows_lock_state()
        if state is True:
            logging.info(
                "시작 시 세션 잠금 — 부팅 출근 생략 (잠금 해제 시 처리)"
            )
            return
        if state is not False:
            logging.info("시작 시 잠금 상태 불명 — 부팅 출근 생략")
            return

        message = self._trigger_check_in_if_allowed(now, trigger="부팅/로그인")
        if message is not None and self._is_ui_visible():
            self.message_label.configure(text=message)

    def _maybe_run_active_hours_start_check_in(
        self,
        now: datetime,
        *,
        within: bool,
        lock_state: Optional[bool] = None,
    ) -> Optional[str]:
        """
        업무시간 밖 → 안 진입 시 오늘 미출근이면 1회 출근 시도.
        예: 08:20 로그인 후 08:30이 되면 출근.
        """
        previous = self._was_within_active_hours
        self._was_within_active_hours = within

        # 당일 이미 시도했으면 전환 감시만 유지
        if self._active_start_check_in_date == now.date():
            return None

        if previous is not False or not within:
            return None

        self._active_start_check_in_date = now.date()

        if not has_app_setup():
            logging.info("업무시간 시작 출근 생략 — 앱 설정 없음")
            return None

        state = (
            lock_state
            if lock_state is not None
            else get_windows_lock_state()
        )
        if state is True:
            logging.info(
                "업무시간 시작 시 세션 잠금 — 출근 생략 (잠금 해제 시 처리)"
            )
            return None
        if state is not False:
            logging.info("업무시간 시작 시 잠금 상태 불명 — 출근 생략")
            return None

        return self._trigger_check_in_if_allowed(now, trigger="업무시간 시작")

    def _maybe_retry_failed_check_in(
        self,
        now: datetime,
        *,
        within: bool,
        lock_state: Optional[bool],
        last_check_in: Optional[date],
    ) -> Optional[str]:
        """실패·unknown 후 재시도 시각이 되면 출근을 다시 시도."""
        if last_check_in == now.date():
            return None
        if not within:
            return None
        if lock_state is True:
            return None
        if not is_retry_due("check_in", now=now):
            return None
        return self._trigger_check_in_if_allowed(now, trigger="출근 재시도")

    def _next_monitor_interval_ms(
        self,
        now: datetime,
        *,
        lock_state: Optional[bool],
        within: bool,
        active_start: dt_time,
        last_check_in: Optional[date],
        last_check_out: Optional[date],
        non_workday_reason: Optional[str],
    ) -> int:
        checkout_enabled = bool(self.auto_checkout_enabled.get())
        checkout_time = self._get_selected_checkout_time()
        return next_poll_interval_ms(
            now,
            lock_state=lock_state,
            within=within,
            checkout_enabled=checkout_enabled,
            checkout_time=checkout_time,
            checkout_triggered_date=self._checkout_triggered_date,
            non_workday_reason=non_workday_reason,
            last_check_in=last_check_in,
            last_check_out=last_check_out,
            active_start=active_start,
            update_check_enabled=load_update_check_enabled(),
            checkout_retry_due=is_retry_due("check_out", now=now),
        )

    def _apply_last_changed_label(self) -> None:
        """마지막 잠금 상태 변경 시각을 GUI에 반영."""
        if self.last_changed_at is None:
            return
        changed_text = self.last_changed_at.strftime("%Y-%m-%d %H:%M:%S")
        if changed_text == self._last_changed_label:
            return
        self._last_changed_label = changed_text
        try:
            self.changed_label.configure(text=changed_text)
        except tk.TclError:
            pass

    def _sync_visible_ui(self, now: datetime) -> None:
        """트레이 복원 등 창이 보일 때 라벨을 현재 캐시·상태로 맞춤."""
        self.holiday_label.configure(text=self._holiday_info_text)
        self.check_in_label.configure(text=self._check_in_status_text)
        self.check_out_label.configure(text=self._check_out_status_text)
        self._apply_window_title()
        self._apply_last_changed_label()
        self._set_state_display(
            self.current_state,
            within_hours=is_within_active_hours(now),
            force=True,
        )

    def _update_monitor(self) -> None:
        now = datetime.now()
        today = now.date()
        if self._last_monitor_date != today:
            self._last_monitor_date = today
            self._checkout_triggered_date = None
            # 자정 경과 시 전일 완료 문구가 남지 않도록 요약·라벨을 강제 갱신
            self._check_in_status_text = ""
            self._check_out_status_text = ""
            self._attendance_summary_text = ""

        ui_visible = self._is_ui_visible()

        self._update_holiday_display(now)

        (
            check_in_status,
            check_out_status,
            last_check_in,
            last_check_out,
            non_workday_reason,
        ) = get_monitor_attendance_snapshot(today)
        self._apply_check_in_status(check_in_status)
        self._apply_check_out_status(check_out_status)
        # 출·퇴근 문구가 같아 early-return 된 경우에도 요약(우선순위)은 맞춤
        self._refresh_attendance_summary(today)

        self._maybe_run_auto_checkout(now)
        self._maybe_run_daily_update_check(now)

        active_hours = load_active_hours()
        within = is_within_active_hours(now, hours=active_hours)
        state = get_windows_lock_state()
        active_start_message = self._maybe_run_active_hours_start_check_in(
            now, within=within, lock_state=state
        )

        action_message: Optional[str] = active_start_message
        if action_message is None:
            action_message = self._maybe_retry_failed_check_in(
                now,
                within=within,
                lock_state=state,
                last_check_in=last_check_in,
            )

        state_changed = state != self.current_state

        if state_changed:
            previous_state = self.current_state
            self.current_state = state
            self.last_changed_at = now

            state_text = {
                True: "잠금 상태",
                False: "잠금 해제",
                None: "확인 불가",
            }[state]

            previous_text = {
                True: "잠금 상태",
                False: "잠금 해제",
                None: "확인 불가",
            }[previous_state]

            logging.info(
                "상태 변경: %s -> %s",
                previous_text,
                state_text,
            )

            if previous_state is True and state is False:
                action_message = self._trigger_check_in_if_allowed(
                    now,
                    trigger="잠금 해제",
                )

        if ui_visible:
            self._set_state_display(state, within_hours=within)
            if action_message is not None:
                self.message_label.configure(text=action_message)
            if state_changed:
                self._apply_last_changed_label()
        else:
            self._last_ui_lock_state = state
            self._last_ui_within_hours = within

        interval = self._next_monitor_interval_ms(
            now,
            lock_state=state,
            within=within,
            active_start=active_hours[0],
            last_check_in=last_check_in,
            last_check_out=last_check_out,
            non_workday_reason=non_workday_reason,
        )
        self.after_id = self.after(interval, self._update_monitor)

    def _on_unmap(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._hiding_to_tray:
            return
        if self.state() == "iconic":
            self.after(0, self._minimize_to_tray)

    def _minimize_to_tray(self) -> None:
        self._hiding_to_tray = True
        try:
            if self.state() != "withdrawn":
                self.withdraw()
            self._start_tray()
            logging.info("창을 시스템 트레이로 최소화")
        finally:
            self._hiding_to_tray = False

    def _start_tray(self) -> None:
        if self._tray_icon is not None:
            return

        # 최신 출퇴근 요약을 반영한 뒤 트레이 라벨 생성
        self._attendance_summary_text = get_tray_status_text()
        self._check_in_status_text = get_check_in_status_text()
        self._check_out_status_text = get_check_out_status_text()
        self.check_in_label.configure(text=self._check_in_status_text)
        self.check_out_label.configure(text=self._check_out_status_text)
        self._apply_window_title()

        menu = pystray.Menu(
            pystray.MenuItem(
                lambda item: self._attendance_summary_text,
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("열기", self._tray_show, default=True),
            pystray.MenuItem("업데이트 확인", self._tray_check_update),
            pystray.MenuItem("종료", self._tray_quit),
        )
        self._tray_icon = pystray.Icon(
            "startofwork",
            create_tray_image(),
            self._status_label_text(),
            menu,
        )
        self._tray_thread = threading.Thread(
            target=self._tray_icon.run,
            name="tray-icon",
            daemon=True,
        )
        self._tray_thread.start()

    def _stop_tray(self) -> None:
        icon = self._tray_icon
        self._tray_icon = None
        self._tray_thread = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logging.exception("트레이 아이콘 종료 실패")

    def _tray_show(self, icon: Optional[pystray.Icon] = None, item=None) -> None:
        self.after(0, self._restore_from_tray)

    def _tray_quit(self, icon: Optional[pystray.Icon] = None, item=None) -> None:
        self.after(0, self._quit_application)

    def _restore_from_tray(self) -> None:
        self._stop_tray()
        self.deiconify()
        self.lift()
        self.focus_force()
        self._sync_visible_ui(datetime.now())
        logging.info("트레이에서 창 복원")

    def _ask_close_action(self) -> str:
        """창 닫기 선택: tray / quit / cancel."""
        result = {"value": "cancel"}

        dialog = tk.Toplevel(self)
        dialog.title(APP_TITLE)
        dialog.resizable(False, False)
        dialog.transient(self)
        dialog.grab_set()
        try:
            if APP_ICON_FILE.is_file():
                dialog.iconbitmap(default=str(APP_ICON_FILE))
        except tk.TclError:
            pass

        frame = ttk.Frame(dialog, padding=(20, 18, 20, 18))
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text="창을 닫으면 어떻게 할까요?",
            style="Info.TLabel",
        ).pack(anchor="w", pady=(0, 6))
        ttk.Label(
            frame,
            text="트레이로 보내면 백그라운드에서 출근·퇴근 감시가 계속됩니다.",
            style="Info.TLabel",
            wraplength=360,
        ).pack(anchor="w", pady=(0, 16))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")

        def _choose(value: str) -> None:
            result["value"] = value
            dialog.grab_release()
            dialog.destroy()

        ttk.Button(
            buttons,
            text="트레이로 이동",
            command=lambda: _choose("tray"),
        ).pack(side="left")
        ttk.Button(
            buttons,
            text="종료",
            command=lambda: _choose("quit"),
        ).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="취소",
            command=lambda: _choose("cancel"),
        ).pack(side="right")

        dialog.protocol("WM_DELETE_WINDOW", lambda: _choose("cancel"))
        dialog.bind("<Escape>", lambda _e: _choose("cancel"))

        dialog.update_idletasks()
        width = dialog.winfo_reqwidth()
        height = dialog.winfo_reqheight()
        parent_x = self.winfo_rootx()
        parent_y = self.winfo_rooty()
        parent_w = max(self.winfo_width(), 1)
        parent_h = max(self.winfo_height(), 1)
        x = parent_x + max((parent_w - width) // 2, 0)
        y = parent_y + max((parent_h - height) // 2, 0)
        dialog.geometry(f"+{x}+{y}")
        dialog.wait_window()
        return result["value"]

    def _on_close(self) -> None:
        """GUI 닫기 버튼 — 트레이 이동 또는 종료를 확인."""
        if self._is_ui_visible():
            choice = self._ask_close_action()
            if choice == "tray":
                logging.info("사용자 선택 — 트레이로 최소화")
                self._minimize_to_tray()
                return
            if choice != "quit":
                logging.info("사용자 선택 — 창 닫기 취소")
                return
        self._quit_application()

    def _quit_application(self) -> None:
        set_notification_handler(None)
        if self.after_id is not None:
            try:
                self.after_cancel(self.after_id)
            except tk.TclError:
                pass

        self._stop_tray()
        logging.info("프로그램 종료")
        self.destroy()

    def destroy(self) -> None:
        set_notification_handler(None)
        super().destroy()

