"""GitHub Releases 기반 자동 업데이트 (방안 A)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from startofwork.constants import (
    APP_VERSION,
    GITHUB_RELEASES_LATEST_URL,
    UPDATE_SETUP_NAME_TEMPLATE,
    UPDATE_USER_AGENT,
)

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
    try:
        with urlopen(request, timeout=timeout_sec) as response:
            data = response.read()
    except HTTPError as exc:
        raise UpdateError(f"다운로드 실패 (HTTP {exc.code})") from exc
    except URLError as exc:
        raise UpdateError("다운로드 실패 — 네트워크 오류") from exc
    except Exception as exc:
        raise UpdateError("다운로드 실패") from exc

    temp_path.write_bytes(data)
    temp_path.replace(dest_path)
    logging.info("업데이트 다운로드 완료: %s (%d bytes)", dest_path, dest_path.stat().st_size)
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


def launch_update_installer(setup_path: Path, *, pid: Optional[int] = None) -> None:
    """설치 파일을 백그라운드로 실행하고 현재 프로세스 종료를 기다린 뒤 재시작."""
    setup_path = setup_path.resolve()
    if not setup_path.is_file():
        raise UpdateError(f"설치 파일이 없습니다: {setup_path}")

    current_pid = pid or os.getpid()
    installed_exe = get_installed_exe_path()
    script_dir = get_update_download_dir()
    bat_path = script_dir / "apply_update.bat"

    bat_lines = [
        "@echo off",
        "setlocal",
        f"set TARGET_PID={current_pid}",
        f"set SETUP={setup_path}",
        f"set EXE={installed_exe}",
        ":wait_loop",
        'tasklist /FI "PID eq %TARGET_PID%" 2>nul | find "%TARGET_PID%" >nul',
        "if %ERRORLEVEL%==0 (",
        "  timeout /t 1 /nobreak >nul",
        "  goto wait_loop",
        ")",
        'if not exist "%SETUP%" exit /b 1',
        '"%SETUP%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
        "if %ERRORLEVEL% NEQ 0 exit /b %ERRORLEVEL%",
        'if exist "%EXE%" start "" "%EXE%"',
        "del /f /q \"%~f0\" >nul 2>&1",
        "endlocal",
    ]
    bat_path.write_text("\r\n".join(bat_lines) + "\r\n", encoding="cp949", errors="replace")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )

    subprocess.Popen(
        ["cmd.exe", "/c", str(bat_path)],
        creationflags=creationflags,
        close_fds=True,
    )
    logging.info(
        "업데이트 설치 스크립트 시작: pid=%s setup=%s",
        current_pid,
        setup_path,
    )


def download_and_prepare_update(release: ReleaseInfo) -> Path:
    path = download_release_asset(release)
    verify_download(path, release.expected_sha256)
    return path
