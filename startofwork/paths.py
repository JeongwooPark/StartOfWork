"""앱 디렉터리·설정 파일 경로·로깅 초기화."""

from __future__ import annotations

import logging
import re
import sys
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PACKAGE_DIR.parent

LOG_RETENTION_DAYS = 7
_LOG_LINE_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
)


def get_app_directory() -> Path:
    """설정·상태 파일이 위치한 폴더 (exe 옆 또는 프로젝트 루트)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return _PROJECT_DIR


def resource_path(filename: str) -> Path:
    """번들 리소스(아이콘 등) 경로. PyInstaller _MEIPASS 우선."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / filename
            if bundled.is_file():
                return bundled
        beside = Path(sys.executable).resolve().parent / filename
        if beside.is_file():
            return beside
    return get_app_directory() / filename


APP_DIR = get_app_directory()
LOG_FILE = APP_DIR / "lock_state_monitor.log"
CHECK_IN_STATE_FILE = APP_DIR / "check_in_state.json"
HOLIDAY_CACHE_FILE = APP_DIR / "holiday_cache.json"
CHROME_PROFILE_DIR = APP_DIR / "chrome_profile"
APP_ICON_FILE = resource_path("StartOfWork.ico")
CONFIG_FILE = APP_DIR / "config.json"


def package_asset(*parts: str) -> Path:
    """패키지 내 정적 자산 경로 (개발·PyInstaller 공통)."""
    relative = Path(*parts)
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = Path(meipass) / relative
            if bundled.exists():
                return bundled
        beside = Path(sys.executable).resolve().parent / relative
        if beside.exists():
            return beside
    return _PACKAGE_DIR / relative


ICONS_DIR = package_asset("assets", "icons")


def prune_log_to_retention(
    path: Path,
    *,
    keep_days: int = LOG_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    """로그 파일에서 keep_days보다 오래된 줄을 제거하고, 남은 줄 수를 반환."""
    if not path.is_file():
        return 0

    current = now or datetime.now()
    cutoff = current - timedelta(days=keep_days)
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if not raw:
        return 0

    kept: list[str] = []
    for line in raw.splitlines(keepends=True):
        match = _LOG_LINE_TS.match(line)
        if match is None:
            if kept:
                kept.append(line)
            continue
        try:
            ts = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            if kept:
                kept.append(line)
            continue
        if ts >= cutoff:
            kept.append(line)

    new_text = "".join(kept)
    if new_text != raw:
        try:
            path.write_text(new_text, encoding="utf-8")
        except OSError:
            return len(kept)
    return len(kept)


def _remove_stale_rotated_logs(
    log_path: Path,
    *,
    keep_days: int = LOG_RETENTION_DAYS,
    now: datetime | None = None,
) -> None:
    """TimedRotating 백업(lock_state_monitor.log.YYYY-MM-DD) 중 오래된 파일 삭제."""
    current = now or datetime.now()
    cutoff = (current - timedelta(days=keep_days)).date()
    prefix = f"{log_path.name}."
    for candidate in log_path.parent.glob(f"{log_path.name}.*"):
        suffix = candidate.name[len(prefix) :]
        try:
            file_day = datetime.strptime(suffix[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if file_day < cutoff:
            try:
                candidate.unlink()
            except OSError:
                pass


def setup_logging() -> None:
    """파일 로깅 한 번만 설정. 일 단위 로테이션 + 약 7일 유지."""
    root = logging.getLogger()
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return

    prune_log_to_retention(LOG_FILE)
    _remove_stale_rotated_logs(LOG_FILE)

    handler = TimedRotatingFileHandler(
        filename=str(LOG_FILE),
        when="midnight",
        interval=1,
        backupCount=LOG_RETENTION_DAYS,
        encoding="utf-8",
        utc=False,
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    handler.setLevel(logging.INFO)

    root.setLevel(logging.INFO)
    root.addHandler(handler)
