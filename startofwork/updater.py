"""GitHub Releases 기반 자동 업데이트 (방안 A)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from startofwork.constants import (
    APP_VERSION,
    GITHUB_RELEASES_LATEST_URL,
    UPDATE_SETUP_NAME_TEMPLATE,
    UPDATE_USER_AGENT,
)

ProgressCallback = Callable[[int, int], None]

_SETUP_SHA_RE = re.compile(
    r"StartOfWorkSetup-[\d.]+\.exe[`'\"]?\s*:\s*[`'\"]?([A-Fa-f0-9]{64})",
    re.IGNORECASE,
)
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$", re.IGNORECASE)


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
    """업데이트 조회·다운로드·적용 실패."""


def parse_version(text: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        raise ValueError(f"버전 형식이 올바르지 않습니다: {text!r}")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def is_newer(remote_version: str, local_version: str = APP_VERSION) -> bool:
    return parse_version(remote_version) > parse_version(local_version)


def parse_setup_sha256(release_body: str, asset_name: str) -> Optional[str]:
    if not release_body:
        return None
    match = _SETUP_SHA_RE.search(release_body)
    if match:
        return match.group(1).upper()
    for line in release_body.splitlines():
        if asset_name in line:
            inline = re.search(r"([A-Fa-f0-9]{64})", line)
            if inline:
                return inline.group(1).upper()
    return None


def pick_setup_asset(
    assets: list[dict[str, Any]], version: str
) -> Optional[dict[str, Any]]:
    preferred = UPDATE_SETUP_NAME_TEMPLATE.format(version=version)
    for asset in assets:
        name = str(asset.get("name", ""))
        if name == preferred:
            return asset
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.startswith("StartOfWorkSetup-") and name.endswith(".exe"):
            return asset
    return None


def release_info_from_json(data: dict[str, Any]) -> Optional[ReleaseInfo]:
    if data.get("draft"):
        return None
    if data.get("prerelease"):
        return None

    tag_name = str(data.get("tag_name", "")).strip()
    if not tag_name:
        return None
    try:
        version = ".".join(str(x) for x in parse_version(tag_name))
    except ValueError:
        logging.warning("릴리스 태그 파싱 실패: %s", tag_name)
        return None

    assets = data.get("assets") or []
    if not isinstance(assets, list):
        assets = []
    asset = pick_setup_asset(assets, version)
    if asset is None:
        logging.warning("릴리스 %s에 설치 파일 자산 없음", tag_name)
        return None

    asset_name = str(asset.get("name", ""))
    download_url = str(asset.get("browser_download_url", "")).strip()
    if not asset_name or not download_url:
        return None

    body = str(data.get("body", "") or "")
    return ReleaseInfo(
        version=version,
        tag_name=tag_name,
        html_url=str(data.get("html_url", "") or ""),
        asset_name=asset_name,
        download_url=download_url,
        body=body,
        expected_sha256=parse_setup_sha256(body, asset_name),
    )


def fetch_latest_release(
    *,
    url: str = GITHUB_RELEASES_LATEST_URL,
    timeout_sec: float = 15.0,
) -> Optional[ReleaseInfo]:
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": UPDATE_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            import json

            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            logging.info("GitHub Releases latest 없음 (404)")
            return None
        raise UpdateError(f"릴리스 조회 실패 (HTTP {exc.code})") from exc
    except URLError as exc:
        raise UpdateError("릴리스 조회 실패 — 네트워크 오류") from exc
    except Exception as exc:
        raise UpdateError("릴리스 조회 실패") from exc

    if not isinstance(payload, dict):
        raise UpdateError("릴리스 응답 형식 오류")
    return release_info_from_json(payload)


def check_for_update(
    local_version: str = APP_VERSION,
) -> tuple[Optional[ReleaseInfo], str]:
    """최신 릴리스 조회. (ReleaseInfo|None, 상태 메시지) 반환."""
    release = fetch_latest_release()
    if release is None:
        return None, "사용 가능한 릴리스가 없습니다."
    if not is_newer(release.version, local_version):
        return None, f"현재 버전({local_version})이 최신입니다."
    return release, f"새 버전 {release.version} 사용 가능"


def get_update_download_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "StartOfWorkUpdate"
    path.mkdir(parents=True, exist_ok=True)
    return path


def download_release_asset(
    release: ReleaseInfo,
    *,
    dest_dir: Optional[Path] = None,
    timeout_sec: float = 120.0,
    progress_callback: Optional[ProgressCallback] = None,
) -> Path:
    """릴리스 자산을 스트리밍 다운로드. progress_callback(downloaded, total).

    total이 없으면(헤더 없음) total=0 으로 콜백한다.
    """
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
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            total_header = response.headers.get("Content-Length")
            try:
                total = int(total_header) if total_header else 0
            except ValueError:
                total = 0
            downloaded = 0
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


def get_installed_exe_path() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        raise UpdateError("LOCALAPPDATA 경로를 찾을 수 없습니다.")
    return Path(local_app_data) / "StartOfWork" / "StartOfWork.exe"


def get_standalone_updater_exe() -> Path:
    """설치본 독립 업데이터 경로."""
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        raise UpdateError("LOCALAPPDATA 경로를 찾을 수 없습니다.")
    return (
        Path(local_app_data) / "StartOfWork" / "Updater" / "StartOfWorkUpdater.exe"
    )


def prepare_setup_for_install(setup_path: Path) -> Path:
    """Temp 다운로드본을 설치 폴더 PendingUpdate로 복사한다."""
    setup_path = setup_path.resolve()
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return setup_path
    pending_dir = Path(local_app_data) / "StartOfWork" / "PendingUpdate"
    pending_dir.mkdir(parents=True, exist_ok=True)
    dest = pending_dir / setup_path.name
    if dest.resolve() != setup_path:
        shutil.copy2(setup_path, dest)
    return dest


# 하위 호환 별칭
_prepare_setup_for_install = prepare_setup_for_install


def launch_standalone_updater(
    release: ReleaseInfo,
    *,
    pid: Optional[int] = None,
    install_exe: Optional[Path] = None,
) -> None:
    """독립형 StartOfWorkUpdater를 ShellExecute로 기동한다."""
    if sys.platform != "win32":
        raise UpdateError("Windows에서만 업데이트를 적용할 수 있습니다.")

    updater_exe = get_standalone_updater_exe()
    if not updater_exe.is_file():
        # 개발/포터블: 메인 exe 옆 Updater
        if getattr(sys, "frozen", False):
            sibling = Path(sys.executable).resolve().parent / "Updater" / (
                "StartOfWorkUpdater.exe"
            )
            if sibling.is_file():
                updater_exe = sibling
        if not updater_exe.is_file():
            raise UpdateError(
                "업데이터 프로그램이 없습니다.\n"
                f"경로: {updater_exe}\n"
                "StartOfWorkSetup을 다시 설치해 주세요."
            )

    current_pid = int(pid or os.getpid())
    main_exe = str(install_exe or get_installed_exe_path())

    def _win_quote(value: str) -> str:
        if not value:
            return '""'
        if any(ch in value for ch in (' ', '\t', '"')):
            return '"' + value.replace('"', '\\"') + '"'
        return value

    parts = [
        "--version",
        _win_quote(release.version),
        "--download-url",
        _win_quote(release.download_url),
        "--asset-name",
        _win_quote(release.asset_name),
        "--html-url",
        _win_quote(release.html_url or ""),
        "--pid",
        str(current_pid),
        "--install-exe",
        _win_quote(main_exe),
    ]
    if release.expected_sha256:
        parts.extend(["--sha256", _win_quote(release.expected_sha256)])
    params = " ".join(parts)

    import ctypes

    rc = int(
        ctypes.windll.shell32.ShellExecuteW(
            None,
            "open",
            str(updater_exe),
            params,
            str(updater_exe.parent),
            1,  # SW_SHOWNORMAL — 진행률 창 표시
        )
    )
    if rc <= 32:
        raise UpdateError(f"업데이터 기동 실패 (ShellExecute={rc})")
    logging.info(
        "독립 업데이터 시작: exe=%s version=%s pid=%s",
        updater_exe,
        release.version,
        current_pid,
    )


def download_and_prepare_update(
    release: ReleaseInfo,
    *,
    progress_callback: Optional[ProgressCallback] = None,
) -> Path:
    path = download_release_asset(
        release, progress_callback=progress_callback
    )
    if progress_callback is not None:
        size = path.stat().st_size
        progress_callback(size, size)
    verify_download(path, release.expected_sha256)
    return path