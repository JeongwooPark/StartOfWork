"""단위/스모크 테스트 — 외부 브라우저·실로그인 불필요."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, time as dt_time
from pathlib import Path
from unittest import mock

# 프로젝트 루트를 path에 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class TestConfigCredentials(unittest.TestCase):
    def test_missing_credentials(self) -> None:
        from startofwork.config import is_missing_credentials

        self.assertTrue(is_missing_credentials("", ""))
        self.assertTrue(is_missing_credentials("아이디", "비밀번호"))
        self.assertFalse(is_missing_credentials("user", "pass"))

    def test_parse_hhmm(self) -> None:
        from startofwork.config import parse_hhmm
        from startofwork.constants import DEFAULT_AUTO_CHECKOUT_TIME

        self.assertEqual(parse_hhmm("09:30"), dt_time(9, 30))
        self.assertEqual(parse_hhmm("bad"), DEFAULT_AUTO_CHECKOUT_TIME)
        self.assertEqual(parse_hhmm("25:00"), DEFAULT_AUTO_CHECKOUT_TIME)

    def test_save_and_load_credentials(self) -> None:
        from startofwork import config
        from startofwork.constants import DEFAULT_ATTENDANCE_URL

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            with mock.patch.object(config, "CONFIG_FILE", cfg):
                config.clear_config_cache()
                data = config.ensure_app_config()
                self.assertEqual(data["username"], "")
                self.assertEqual(data["attendance_url"], "")
                self.assertFalse(config.has_login_credentials())
                self.assertFalse(config.has_app_setup())

                config.save_app_setup(
                    DEFAULT_ATTENDANCE_URL, "alice", "secret"
                )
                self.assertTrue(config.has_login_credentials())
                self.assertTrue(config.has_app_setup())
                user, pw = config.load_login_credentials()
                self.assertEqual((user, pw), ("alice", "secret"))
                self.assertEqual(
                    config.load_attendance_url(), DEFAULT_ATTENDANCE_URL
                )

                loaded = json.loads(cfg.read_text(encoding="utf-8"))
                self.assertEqual(loaded["username"], "alice")
                self.assertEqual(loaded["attendance_url"], DEFAULT_ATTENDANCE_URL)
                self.assertIn("auto_checkout_time", loaded)
                self.assertIn("active_start_time", loaded)

    def test_missing_attendance_url(self) -> None:
        from startofwork.config import is_missing_attendance_url

        self.assertTrue(is_missing_attendance_url(""))
        self.assertTrue(is_missing_attendance_url("ftp://example.com"))
        self.assertTrue(is_missing_attendance_url("example.com/path"))
        self.assertFalse(
            is_missing_attendance_url("https://acme.daouoffice.com/ehr/x")
        )
        self.assertFalse(is_missing_attendance_url("http://localhost/attend"))

    def test_migrate_legacy_config_fills_default_url(self) -> None:
        from startofwork import config
        from startofwork.constants import DEFAULT_ATTENDANCE_URL

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            cfg.write_text(
                json.dumps(
                    {
                        "username": "bob",
                        "password": "pw",
                        "active_start_time": "08:30",
                        "active_end_time": "18:00",
                        "auto_checkout_enabled": False,
                        "auto_checkout_time": "18:00",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(config, "CONFIG_FILE", cfg):
                config.clear_config_cache()
                data = config.ensure_app_config()
                self.assertEqual(data["attendance_url"], DEFAULT_ATTENDANCE_URL)
                self.assertTrue(config.has_app_setup())

    def test_active_hours_save_load(self) -> None:
        from startofwork import config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            with mock.patch.object(config, "CONFIG_FILE", cfg):
                config.clear_config_cache()
                config.ensure_app_config()
                config.save_active_hours(dt_time(9, 0), dt_time(17, 30))
                start, end = config.load_active_hours()
                self.assertEqual(start, dt_time(9, 0))
                self.assertEqual(end, dt_time(17, 30))

                # 시작 > 종료이면 기본값
                config.save_active_hours(dt_time(18, 0), dt_time(9, 0))
                start, end = config.load_active_hours()
                self.assertEqual(start, dt_time(8, 30))
                self.assertEqual(end, dt_time(18, 0))

    def test_checkout_save_skips_unchanged(self) -> None:
        from startofwork import config

        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "config.json"
            with mock.patch.object(config, "CONFIG_FILE", cfg):
                config.clear_config_cache()
                config.ensure_app_config()
                config.save_auto_checkout_settings(True, dt_time(18, 0))
                mtime1 = cfg.stat().st_mtime_ns
                with mock.patch.object(config, "save_app_config") as save:
                    config.save_auto_checkout_settings(True, dt_time(18, 0))
                    save.assert_not_called()
                self.assertEqual(cfg.stat().st_mtime_ns, mtime1)


class TestAttendanceState(unittest.TestCase):
    def test_format_and_status(self) -> None:
        from startofwork import attendance_state

        with tempfile.TemporaryDirectory() as tmp:
            state_file = Path(tmp) / "check_in_state.json"
            with mock.patch.object(attendance_state, "CHECK_IN_STATE_FILE", state_file):
                attendance_state.clear_check_in_state_cache()
                with mock.patch(
                    "startofwork.attendance_state.get_non_workday_reason",
                    return_value=None,
                ):
                    today = date(2026, 7, 15)
                    self.assertEqual(
                        attendance_state.get_check_in_status_text(today),
                        "미완료",
                    )
                    attendance_state.save_check_in_date(today)
                    text = attendance_state.get_check_in_status_text(today)
                    self.assertTrue(text.startswith("완료"))
                    self.assertEqual(
                        attendance_state.load_last_check_in_date(), today
                    )


class TestRules(unittest.TestCase):
    def test_active_hours(self) -> None:
        from startofwork.rules import is_within_active_hours

        with mock.patch(
            "startofwork.rules.load_active_hours",
            return_value=(dt_time(8, 30), dt_time(18, 0)),
        ):
            self.assertTrue(
                is_within_active_hours(datetime(2026, 7, 15, 9, 0))
            )
            self.assertFalse(
                is_within_active_hours(datetime(2026, 7, 15, 7, 0))
            )

    def test_should_open_browser(self) -> None:
        from startofwork import rules

        with mock.patch(
            "startofwork.rules.load_active_hours",
            return_value=(dt_time(8, 30), dt_time(18, 0)),
        ), mock.patch(
            "startofwork.rules.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.rules.load_last_check_in_date", return_value=None
        ):
            ok, reason = rules.should_open_browser(datetime(2026, 7, 15, 10, 0))
            self.assertTrue(ok)
            self.assertEqual(reason, "근무일")

            ok, reason = rules.should_open_browser(datetime(2026, 7, 15, 7, 0))
            self.assertFalse(ok)
            self.assertEqual(reason, "활성 시간대 외")

        with mock.patch(
            "startofwork.rules.load_active_hours",
            return_value=(dt_time(8, 30), dt_time(18, 0)),
        ), mock.patch(
            "startofwork.rules.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.rules.load_last_check_in_date",
            return_value=date(2026, 7, 15),
        ):
            ok, reason = rules.should_open_browser(datetime(2026, 7, 15, 10, 0))
            self.assertFalse(ok)
            self.assertEqual(reason, "오늘 출근체크 완료")

    def test_should_attempt_check_out(self) -> None:
        from startofwork import rules

        day = date(2026, 7, 15)
        with mock.patch(
            "startofwork.rules.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.rules.load_last_check_in_date", return_value=day
        ), mock.patch(
            "startofwork.rules.load_last_check_out_date", return_value=None
        ):
            self.assertFalse(
                rules.should_attempt_check_out(
                    day,
                    checkout_time=dt_time(18, 0),
                    now=datetime(2026, 7, 15, 17, 0),
                )
            )
            self.assertTrue(
                rules.should_attempt_check_out(
                    day,
                    checkout_time=dt_time(18, 0),
                    now=datetime(2026, 7, 15, 18, 5),
                )
            )

    def test_check_out_before_time_skips_without_check_in_call(self) -> None:
        from startofwork import rules

        with mock.patch(
            "startofwork.rules.load_last_check_in_date"
        ) as load_in, mock.patch(
            "startofwork.rules.get_non_workday_reason"
        ) as holiday:
            self.assertFalse(
                rules.should_attempt_check_out(
                    date(2026, 7, 16),
                    checkout_time=dt_time(18, 0),
                    now=datetime(2026, 7, 16, 8, 53),
                )
            )
            load_in.assert_not_called()
            holiday.assert_not_called()


class TestStartupCheckIn(unittest.TestCase):
    def test_startup_check_in_once_when_unlocked(self) -> None:
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=False
        ), mock.patch(
            "startofwork.gui.should_open_browser",
            return_value=(True, "근무일"),
        ) as open_ok, mock.patch(
            "startofwork.gui.open_attendance_page", return_value=True
        ) as open_page, mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ):
            app = LockStateMonitor()
            try:
                app._startup_check_in_attempted = False
                app._try_startup_check_in()
                open_ok.assert_called_once()
                open_page.assert_called_once()

                # 프로세스당 1회만
                app._try_startup_check_in()
                self.assertEqual(open_page.call_count, 1)
            finally:
                app.destroy()

    def test_startup_check_in_skips_when_locked(self) -> None:
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=True
        ), mock.patch(
            "startofwork.gui.open_attendance_page", return_value=True
        ) as open_page, mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ):
            app = LockStateMonitor()
            try:
                app._startup_check_in_attempted = False
                app._try_startup_check_in()
                open_page.assert_not_called()
            finally:
                app.destroy()


class TestActiveHoursStartCheckIn(unittest.TestCase):
    def test_triggers_when_entering_active_hours(self) -> None:
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=False
        ), mock.patch(
            "startofwork.gui.should_open_browser",
            return_value=(True, "근무일"),
        ), mock.patch(
            "startofwork.gui.open_attendance_page", return_value=True
        ) as open_page, mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ):
            app = LockStateMonitor()
            try:
                now = datetime(2026, 7, 15, 8, 30)
                # 첫 관측: 업무시간 밖
                self.assertIsNone(
                    app._maybe_run_active_hours_start_check_in(
                        datetime(2026, 7, 15, 8, 20),
                        within=False,
                    )
                )
                open_page.assert_not_called()

                # 업무시간 진입
                msg = app._maybe_run_active_hours_start_check_in(now, within=True)
                self.assertIsNotNone(msg)
                self.assertIn("업무시간 시작", msg)
                open_page.assert_called_once()

                # 같은 날 재진입 없음
                app._was_within_active_hours = False
                again = app._maybe_run_active_hours_start_check_in(now, within=True)
                self.assertIsNone(again)
                self.assertEqual(open_page.call_count, 1)
            finally:
                app.destroy()


class TestHolidays(unittest.TestCase):
    def test_normalize_and_weekend(self) -> None:
        from startofwork.holidays import get_non_workday_reason, normalize_holiday_map

        self.assertEqual(
            normalize_holiday_map({"2026-01-01": "신정"}),
            {"2026-01-01": "신정"},
        )
        with mock.patch(
            "startofwork.holidays.refresh_holiday_cache_if_needed",
            return_value={},
        ):
            self.assertEqual(
                get_non_workday_reason(date(2026, 7, 18)),  # Saturday
                "토요일",
            )
            self.assertEqual(
                get_non_workday_reason(date(2026, 7, 19)),  # Sunday
                "일요일",
            )

    def test_cache_only_skips_api(self) -> None:
        from startofwork import holidays

        with tempfile.TemporaryDirectory() as tmp:
            cache_file = Path(tmp) / "holiday_cache.json"
            cache_file.write_text(
                json.dumps(
                    {
                        "checked_date": "2026-07-14",
                        "year": 2026,
                        "month": 7,
                        "holidays": {"2026-07-17": "제헌절"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with mock.patch.object(holidays, "HOLIDAY_CACHE_FILE", cache_file), mock.patch.object(
                holidays, "fetch_public_holidays"
            ) as fetch:
                holidays.clear_holiday_memory_cache()
                reason = holidays.get_non_workday_reason(
                    date(2026, 7, 17), cache_only=True
                )
                self.assertEqual(reason, "공휴일(제헌절)")
                fetch.assert_not_called()


class TestPollInterval(unittest.TestCase):
    def test_fast_when_check_in_pending_in_hours(self) -> None:
        from startofwork.constants import (
            CHECK_INTERVAL_IDLE_MS,
            CHECK_INTERVAL_MS,
            CHECK_INTERVAL_QUIET_MS,
        )
        from startofwork.gui import next_poll_interval_ms

        now = datetime(2026, 7, 15, 9, 0)
        self.assertEqual(
            next_poll_interval_ms(
                now,
                lock_state=False,
                within=True,
                checkout_enabled=False,
                checkout_time=dt_time(18, 0),
                checkout_triggered_date=None,
                non_workday_reason=None,
                last_check_in=None,
                last_check_out=None,
                active_start=dt_time(8, 30),
            ),
            CHECK_INTERVAL_MS,
        )
        # 출근·퇴근 모두 완료 → 한산
        self.assertEqual(
            next_poll_interval_ms(
                now,
                lock_state=False,
                within=True,
                checkout_enabled=True,
                checkout_time=dt_time(18, 0),
                checkout_triggered_date=date(2026, 7, 15),
                non_workday_reason=None,
                last_check_in=date(2026, 7, 15),
                last_check_out=date(2026, 7, 15),
                active_start=dt_time(8, 30),
            ),
            CHECK_INTERVAL_QUIET_MS,
        )
        # 출근 완료, 퇴근 시각 전이면 idle
        self.assertEqual(
            next_poll_interval_ms(
                datetime(2026, 7, 15, 12, 0),
                lock_state=False,
                within=True,
                checkout_enabled=True,
                checkout_time=dt_time(18, 0),
                checkout_triggered_date=None,
                non_workday_reason=None,
                last_check_in=date(2026, 7, 15),
                last_check_out=None,
                active_start=dt_time(8, 30),
            ),
            CHECK_INTERVAL_IDLE_MS,
        )
        # 새벽 1시 직전 + 업데이트 확인 켜짐 → 빠른 폴링
        self.assertEqual(
            next_poll_interval_ms(
                datetime(2026, 7, 16, 0, 59, 0),
                lock_state=False,
                within=False,
                checkout_enabled=False,
                checkout_time=dt_time(18, 0),
                checkout_triggered_date=None,
                non_workday_reason="주말",
                last_check_in=None,
                last_check_out=None,
                active_start=dt_time(8, 30),
                update_check_enabled=True,
            ),
            CHECK_INTERVAL_MS,
        )


class TestAutoCheckoutTrigger(unittest.TestCase):
    def test_checkout_triggered_date_suppresses_retry(self) -> None:
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=False
        ), mock.patch(
            "startofwork.gui.should_attempt_check_out", return_value=True
        ), mock.patch(
            "startofwork.gui.open_checkout_page", return_value=True
        ) as open_page, mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ):
            app = LockStateMonitor()
            try:
                app.auto_checkout_enabled.set(True)
                now = datetime(2026, 7, 15, 18, 5)
                app._maybe_run_auto_checkout(now)
                app._maybe_run_auto_checkout(now)
                self.assertEqual(open_page.call_count, 1)
                self.assertEqual(app._checkout_triggered_date, now.date())
            finally:
                app.destroy()


class TestNotifications(unittest.TestCase):
    def test_notify_helpers_call_handler(self) -> None:
        from startofwork import notifications

        calls: list[tuple[str, str]] = []
        notifications.set_notification_handler(
            lambda title, message: calls.append((title, message))
        )
        try:
            notifications.notify_check_in_done(date(2026, 7, 15))
            notifications.notify_check_out_done(date(2026, 7, 15))
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][0], "출근 체크 완료")
            self.assertEqual(calls[1][0], "퇴근 체크 완료")
        finally:
            notifications.set_notification_handler(None)

    def test_show_without_handler_is_safe(self) -> None:
        from startofwork import notifications

        notifications.set_notification_handler(None)
        notifications.show_windows_toast("t", "m")  # 예외 없음

    def test_show_handler_error_swallowed(self) -> None:
        from startofwork import notifications

        def boom(_title: str, _message: str) -> None:
            raise RuntimeError("boom")

        notifications.set_notification_handler(boom)
        try:
            notifications.show_windows_toast("t", "m")
        finally:
            notifications.set_notification_handler(None)


class TestCloseDialog(unittest.TestCase):
    def test_close_asks_then_tray(self) -> None:
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=False
        ), mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor, "_ask_close_action", return_value="tray"
        ), mock.patch.object(
            LockStateMonitor, "_is_ui_visible", return_value=True
        ):
            app = LockStateMonitor()
            try:
                with mock.patch.object(app, "_minimize_to_tray") as to_tray, mock.patch.object(
                    app, "_quit_application"
                ) as quit_app:
                    app._on_close()
                    to_tray.assert_called_once()
                    quit_app.assert_not_called()
            finally:
                app.destroy()

    def test_close_asks_then_quit(self) -> None:
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=False
        ), mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor, "_ask_close_action", return_value="quit"
        ), mock.patch.object(
            LockStateMonitor, "_is_ui_visible", return_value=True
        ):
            app = LockStateMonitor()
            try:
                with mock.patch.object(app, "_minimize_to_tray") as to_tray, mock.patch.object(
                    app, "_quit_application"
                ) as quit_app:
                    app._on_close()
                    to_tray.assert_not_called()
                    quit_app.assert_called_once()
            finally:
                app.destroy()


class TestLogRetention(unittest.TestCase):
    def test_prune_keeps_only_recent_days(self) -> None:
        from startofwork.paths import prune_log_to_retention

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "lock_state_monitor.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-07-01 10:00:00,000 [INFO] old",
                        "2026-07-10 10:00:00,000 [INFO] mid",
                        "2026-07-16 09:00:00,000 [INFO] new",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            kept = prune_log_to_retention(
                log_path,
                keep_days=7,
                now=datetime(2026, 7, 16, 12, 0, 0),
            )
            text = log_path.read_text(encoding="utf-8")
            self.assertNotIn("old", text)
            self.assertIn("mid", text)
            self.assertIn("new", text)
            self.assertGreaterEqual(kept, 2)


class TestLockState(unittest.TestCase):
    def test_lock_state_callable(self) -> None:
        from startofwork.lock_state import get_windows_lock_state

        state = get_windows_lock_state()
        self.assertIn(state, (True, False, None))


class TestBrowserHelpers(unittest.TestCase):
    def test_find_chrome(self) -> None:
        from startofwork.browser import find_chrome_executable

        chrome = find_chrome_executable()
        # 설치 환경에 따라 None일 수 있으나 호출은 성공해야 함
        self.assertTrue(chrome is None or chrome.is_file())

    def test_button_xpath_tuples(self) -> None:
        from startofwork.browser import (
            ATTENDANCE_ACTION_XPATHS,
            CHECK_IN_BUTTON_XPATHS,
            CHECK_OUT_BUTTON_XPATHS,
        )

        self.assertTrue(any("출근하기" in x for x in CHECK_IN_BUTTON_XPATHS))
        self.assertTrue(any("퇴근하기" in x for x in CHECK_OUT_BUTTON_XPATHS))
        self.assertGreaterEqual(len(ATTENDANCE_ACTION_XPATHS), 4)
        # text 기반이 absolute 경로보다 앞에 와야 함
        self.assertLess(
            next(i for i, x in enumerate(CHECK_IN_BUTTON_XPATHS) if "출근하기" in x),
            next(i for i, x in enumerate(CHECK_IN_BUTTON_XPATHS) if x.startswith("/html")),
        )

    def test_chrome_options_use_profile_dir(self) -> None:
        from startofwork import browser
        from startofwork.paths import CHROME_PROFILE_DIR

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "chrome_profile"
            with mock.patch.object(browser, "CHROME_PROFILE_DIR", profile):
                options = browser.create_chrome_options(Path("C:/fake/chrome.exe"))
                args = options.arguments
                self.assertTrue(
                    any(a.startswith("--user-data-dir=") and str(profile) in a for a in args)
                )
                self.assertTrue(profile.is_dir())
        self.assertEqual(CHROME_PROFILE_DIR.name, "chrome_profile")


class TestSingleInstance(unittest.TestCase):
    def test_second_acquire_fails(self) -> None:
        from startofwork import single_instance as si

        test_mutex = "Local\\StartOfWork_SingleInstance_TestOnly"
        si.release_single_instance()
        self.assertTrue(si.try_acquire_single_instance(test_mutex))
        # 동일 프로세스에서 다시 호출하면 이미 보유 중이므로 True
        self.assertTrue(si.try_acquire_single_instance(test_mutex))

        # 핸들을 비운 뒤 새로 CreateMutex하면 ALREADY_EXISTS
        handle = si._mutex_handle
        si._mutex_handle = None
        self.assertFalse(si.try_acquire_single_instance(test_mutex))
        # 정리: 원래 핸들 복구 후 해제
        si._mutex_handle = handle
        si.release_single_instance()


class TestImportsSmoke(unittest.TestCase):
    def test_import_package_modules(self) -> None:
        import startofwork
        from startofwork import (
            app,
            attendance_state,
            browser,
            config,
            constants,
            gui,
            holidays,
            lock_state,
            notifications,
            paths,
            rules,
        )

        self.assertTrue(hasattr(startofwork, "main"))
        self.assertTrue(hasattr(gui, "LockStateMonitor"))
        self.assertTrue(hasattr(app, "main"))
        self.assertEqual(paths.APP_ICON_FILE.name, "StartOfWork.ico")
        self.assertEqual(constants.APP_TITLE, "출근 근태 자동 실행")
        self.assertEqual(constants.APP_VERSION, "1.2.6")
        self.assertEqual(startofwork.__version__, "1.2.6")
        # 모듈 참조 유지 (미사용 경고 방지)
        self.assertIsNotNone(browser)
        self.assertIsNotNone(config)
        self.assertIsNotNone(holidays)
        self.assertIsNotNone(attendance_state)
        self.assertIsNotNone(rules)
        self.assertIsNotNone(lock_state)
        self.assertTrue(hasattr(notifications, "notify_check_in_done"))

    def test_gui_init_without_mainloop(self) -> None:
        """LockStateMonitor 생성까지 스모크 (즉시 destroy)."""
        from startofwork.gui import LockStateMonitor

        with mock.patch(
            "startofwork.gui.has_app_setup", return_value=True
        ), mock.patch(
            "startofwork.gui.get_non_workday_reason", return_value=None
        ), mock.patch(
            "startofwork.gui.get_windows_lock_state", return_value=False
        ), mock.patch.object(
            LockStateMonitor, "_minimize_to_tray", lambda self: None
        ), mock.patch.object(
            LockStateMonitor, "_update_monitor", lambda self: None
        ), mock.patch.object(
            LockStateMonitor,
            "_refresh_holidays_and_display",
            lambda self, **kwargs: None,
        ), mock.patch.object(
            LockStateMonitor,
            "_schedule_holiday_refresh",
            lambda self, **kwargs: None,
        ):
            app = LockStateMonitor()
            try:
                self.assertTrue(app.title().startswith("출근 근태 자동 실행"))
                self.assertTrue(hasattr(app, "check_in_label"))
            finally:
                app.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
