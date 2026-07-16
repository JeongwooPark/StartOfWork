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


def _ps_single_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def launch_update_installer(setup_path: Path, *, pid: Optional[int] = None) -> None:
    """앱 종료 후 Setup을 실행하는 독립 프로세스를 띄운다.

    배치+timeout은 창 없는 환경에서 즉시 실패하고, 부모 종료 시 자식이
    함께 죽는 경우가 있어 PowerShell을 start로 분리 실행한다.
    """
    setup_path = setup_path.resolve()
    if not setup_path.is_file():
        raise UpdateError(f"설치 파일이 없습니다: {setup_path}")

    current_pid = pid or os.getpid()
    installed_exe = get_installed_exe_path()
    script_dir = get_update_download_dir()
    ps1_path = script_dir / "apply_update.ps1"
    log_path = script_dir / "update.log"

    # /SILENT: 진행 창만 표시 (VERYSILENT는 UI가 없어 실패처럼 보임)
    # CLOSEAPPLICATIONS: 잠긴 exe 교체 유도
    script = "\n".join(
        [
            "$ErrorActionPreference = 'Continue'",
            f"$targetPid = {int(current_pid)}",
            f"$setup = {_ps_single_quote(str(setup_path))}",
            f"$exe = {_ps_single_quote(str(installed_exe))}",
            f"$log = {_ps_single_quote(str(log_path))}",
            "function Write-UpdateLog([string]$Message) {",
            "  $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message",
            "  Add-Content -LiteralPath $log -Value $line -Encoding UTF8",
            "}",
            "Write-UpdateLog ('start pid=' + $targetPid)",
            "Write-UpdateLog ('setup=' + $setup)",
            "$waited = 0",
            "while ($waited -lt 120) {",
            "  $proc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue",
            "  if ($null -eq $proc) { break }",
            "  Start-Sleep -Seconds 1",
            "  $waited++",
            "}",
            "Write-UpdateLog ('app exited after ' + $waited + 's')",
            "Start-Sleep -Seconds 2",
            "if (-not (Test-Path -LiteralPath $setup)) {",
            "  Write-UpdateLog 'setup file missing'",
            "  exit 1",
            "}",
            "Write-UpdateLog 'launching setup'",
            "$setupArgs = @('/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS')",
            "$p = Start-Process -FilePath $setup -ArgumentList $setupArgs -PassThru -Wait",
            "Write-UpdateLog ('setup exitCode=' + $p.ExitCode)",
            "if ($p.ExitCode -ne 0) { exit $p.ExitCode }",
            "Start-Sleep -Seconds 1",
            "if (Test-Path -LiteralPath $exe) {",
            "  Write-UpdateLog 'restarting app'",
            "  Start-Process -FilePath $exe",
            "} else {",
            "  Write-UpdateLog 'installed exe missing'",
            "}",
            "Write-UpdateLog 'done'",
        ]
    )
    ps1_path.write_text(script + "\n", encoding="utf-8")

    # cmd `start "Title"` 는 환경에 따라 Title을 실행 경로로 오인할 수 있음
    # (오류: '\StartOfWorkUpdate\'을(를) 찾을 수 없습니다)
    # PowerShell Start-Process 로 완전 분리 기동한다.
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    creationflags = (
        CREATE_NO_WINDOW
        | CREATE_NEW_PROCESS_GROUP
        | DETACHED_PROCESS
        | CREATE_BREAKAWAY_FROM_JOB
    )

    # 바깥 PowerShell이 즉시 반환되도록 Start-Process만 호출
    outer = (
        "Start-Process -FilePath 'powershell.exe' "
        "-ArgumentList @("
        "'-NoProfile',"
        "'-ExecutionPolicy',"
        "'Bypass',"
        "'-WindowStyle',"
        "'Hidden',"
        "'-File',"
        f"{_ps_single_quote(str(ps1_path))}"
        ") -WindowStyle Hidden"
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-Command",
            outer,
        ],
        creationflags=creationflags if sys.platform == "win32" else 0,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    logging.info(
        "업데이트 설치 스크립트 시작: pid=%s setup=%s log=%s",
        current_pid,
        setup_path,
        log_path,
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
        # 검증 단계 표시용: total=total, downloaded=total 유지
        size = path.stat().st_size
        progress_callback(size, size)
    verify_download(path, release.expected_sha256)
    return path
