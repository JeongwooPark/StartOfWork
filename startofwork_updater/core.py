"""업데이터 전용 다운로드·경로 유틸 (startofwork 패키지 비의존)."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UPDATE_USER_AGENT = "StartOfWorkUpdater/1.2.15"
ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    tag_name: str
    html_url: str
    asset_name: str
    download_url: str
    body: str
    expected_sha256: Optional[str] = None


class UpdateError(Exception):
    """업데이트 다운로드·적용 실패."""


def get_update_download_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "StartOfWorkUpdate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_pending_update_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        path = get_update_download_dir() / "PendingUpdate"
    else:
        path = Path(local_app_data) / "StartOfWork" / "PendingUpdate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_installed_exe_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        raise UpdateError("LOCALAPPDATA 경로를 찾을 수 없습니다.")
    return Path(local_app_data) / "StartOfWork" / "StartOfWork.exe"


def download_release_asset(
    release: ReleaseInfo,
    *,
    dest_dir: Optional[Path] = None,
    timeout_sec: float = 120.0,
    progress_callback: Optional[ProgressCallback] = None,
) -> Path:
    target_dir = dest_dir or get_update_download_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_path = target_dir / release.asset_name
    temp_path = dest_path.with_suffix(dest_path.suffix + ".part")

    request = Request(
        release.download_url,
        headers={"User-Agent": UPDATE_USER_AGENT},
        method="GET",
    )
    logging.info("업데이트 다운로드 시작: %s", release.asset_name)
    total = 0
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            total_header = response.headers.get("Content-Length")
            try:
                total = int(total_header) if total_header else 0
            except ValueError:
                total = 0
            downloaded = 0
            last_progress_at = 0.0
            if progress_callback is not None:
                progress_callback(0, total)
            with temp_path.open("wb") as out:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback is not None:
                        now = time.monotonic()
                        if (
                            now - last_progress_at >= 0.15
                            or (total > 0 and downloaded >= total)
                        ):
                            last_progress_at = now
                            progress_callback(downloaded, total)
    except HTTPError as exc:
        raise UpdateError(f"다운로드 실패 (HTTP {exc.code})") from exc
    except URLError as exc:
        raise UpdateError("다운로드 실패 — 네트워크 오류") from exc
    except Exception as exc:
        raise UpdateError("다운로드 실패") from exc

    if not temp_path.is_file() or temp_path.stat().st_size <= 0:
        raise UpdateError("다운로드된 파일이 비어 있습니다.")
    temp_path.replace(dest_path)
    if progress_callback is not None:
        size = dest_path.stat().st_size
        progress_callback(size, size if total <= 0 else total)
    logging.info(
        "업데이트 다운로드 완료: %s (%d bytes)",
        dest_path,
        dest_path.stat().st_size,
    )
    return dest_path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def verify_download(path: Path, expected_sha256: Optional[str]) -> None:
    if not expected_sha256:
        logging.info("릴리스에 SHA256 없음 — 해시 검증 생략")
        return
    actual = sha256_file(path)
    expected = expected_sha256.strip().upper()
    if actual != expected:
        raise UpdateError(
            "다운로드 파일 해시가 일치하지 않습니다. "
            f"expected={expected} actual={actual}"
        )
    logging.info("다운로드 SHA256 검증 완료")


def prepare_setup_for_install(setup_path: Path) -> Path:
    setup_path = setup_path.resolve()
    pending_dir = get_pending_update_dir().resolve()
    if setup_path.parent == pending_dir:
        return setup_path
    dest = pending_dir / setup_path.name
    if dest.resolve() != setup_path:
        shutil.copy2(setup_path, dest)
    return dest


def download_and_prepare_update(
    release: ReleaseInfo,
    *,
    progress_callback: Optional[ProgressCallback] = None,
    dest_dir: Optional[Path] = None,
) -> Path:
    target = dest_dir or get_pending_update_dir()
    path = download_release_asset(
        release,
        dest_dir=target,
        progress_callback=progress_callback,
    )
    if progress_callback is not None:
        size = path.stat().st_size
        progress_callback(size, size)
    verify_download(path, release.expected_sha256)
    return path
